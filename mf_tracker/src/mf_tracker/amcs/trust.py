from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import polars as pl

from ..domain import FundSnapshot, Holding, IngestionIssue, ParsedWorkbook
from ..errors import ValidationError
from ..workbooks import RawWorkbook, read_workbook

PARSER_VERSION = "trust-v1"
AMC_SLUG = "trust"
AMC_NAME = "TRUST Mutual Fund"
EQUITY_START_SHEET = "TMFFLEXI"
FUND_NAMES = {
    "TMFFLEXI": "TRUSTMF Flexi Cap Fund",
    "TMFSCAP": "TRUSTMF Small Cap Fund",
    "TMFMCAP": "TRUSTMF Multi Cap Fund",
    "TMFARB": "TRUSTMF Arbitrage Fund",
    "TMFMID": "TRUSTMF Mid Cap Fund",
}
SHEET_CODES = set(FUND_NAMES)
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
REPORT_DATE_RE = re.compile(r"monthly portfolio statement as on\s+(.+)$", re.I)


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()
    return text or None


def normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def number(value: Any, *, percent: bool = False) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
    else:
        text = clean_text(value)
        if text is None or text.upper() in {"NIL", "NA", "N/A", "-"}:
            return None
        has_percent = text.endswith("%")
        text = text.replace(",", "").replace("₹", "").replace("Rs.", "").strip()
        if has_percent:
            text = text[:-1].strip()
        try:
            parsed = float(text)
        except ValueError:
            return None
        if has_percent:
            return parsed / 100.0
    return parsed / 100.0 if percent and abs(parsed) > 1.0 else parsed


def _row(frame: pl.DataFrame, index: int) -> list[Any]:
    values = frame.row(index, named=True)
    return [values.get(name) for name in frame.columns if name.startswith("column_")]


def _text_blob(frame: pl.DataFrame, limit: int = 12) -> str:
    return " ".join(
        clean_text(value) or ""
        for index in range(min(frame.height, limit))
        for value in _row(frame, index)
    ).casefold()


def _has_markers(frame: pl.DataFrame) -> bool:
    text = _text_blob(frame)
    return (
        "trustmf" in text
        and "monthly portfolio statement as on" in text
        and "name of the instrument" in text
        and "isin" in text
    )


def _equity_sheets(raw: RawWorkbook) -> list[tuple[str, pl.DataFrame]]:
    sheets = list(raw.sheets.items())
    start = next(
        (index for index, (name, _) in enumerate(sheets) if name.strip().upper() == EQUITY_START_SHEET),
        None,
    )
    if start is None:
        return []
    return [
        (name.strip().upper(), frame)
        for name, frame in sheets[start:]
        if name.strip().upper() in SHEET_CODES and _has_markers(frame)
    ]


def matches_workbook(raw: RawWorkbook) -> bool:
    return bool(_equity_sheets(raw))


def _date_value(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = clean_text(value)
    if not text:
        return None
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text, flags=re.I)
    text = re.sub(r",\s*", ", ", text)
    for fmt in ("%B %d, %Y", "%B %d %Y", "%d %B %Y", "%d-%B-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _report_date(frame: pl.DataFrame, sheet: str) -> date:
    for index in range(min(frame.height, 15)):
        for value in _row(frame, index):
            text = clean_text(value)
            if not text:
                continue
            match = REPORT_DATE_RE.search(text)
            if match:
                parsed = _date_value(match.group(1))
                if parsed:
                    return parsed
                raise ValidationError(f"{sheet}: invalid monthly portfolio reporting date")
    raise ValidationError(f"{sheet}: missing monthly portfolio reporting date")


def _find_header(frame: pl.DataFrame) -> tuple[int, dict[str, int]]:
    for index in range(min(frame.height, 30)):
        row = _row(frame, index)
        columns: dict[str, int] = {}
        for cell_index, value in enumerate(row):
            label = normalized_name(clean_text(value) or "")
            if "name of the instrument" in label:
                columns["name"] = cell_index
            elif label == "isin":
                columns["isin"] = cell_index
            elif label in {"industry", "rating"}:
                columns["industry"] = cell_index
            elif label == "quantity":
                columns["quantity"] = cell_index
            elif "market fair value" in label and "lakh" in label:
                columns["value"] = cell_index
            elif "net assets" in label:
                columns["weight"] = cell_index
            elif label.startswith("yield to maturity"):
                columns["ytm"] = cell_index
            elif label.startswith("yield to call"):
                columns["ytc"] = cell_index
        required = {"name", "isin", "industry", "quantity", "value", "weight"}
        if required <= columns.keys():
            if columns["name"] > 0:
                columns["code"] = columns["name"] - 1
            return index, columns
    raise ValidationError("missing TRUSTMF portfolio header")


def _cell(row: list[Any], columns: dict[str, int], key: str) -> Any:
    index = columns.get(key, -1)
    return row[index] if 0 <= index < len(row) else None


def _scheme_name(frame: pl.DataFrame, sheet: str) -> str:
    expected = FUND_NAMES[sheet]
    expected_normalized = normalized_name(expected)
    for index in range(min(frame.height, 5)):
        for value in _row(frame, index):
            text = clean_text(value)
            if text and normalized_name(text) == expected_normalized:
                return text
    raise ValidationError(f"{sheet}: missing or unexpected TRUSTMF scheme name {expected!r}")


def _identity(instrument_type: str, isin: str | None, name: str) -> str:
    if isin:
        return f"{instrument_type}:isin:{isin}"
    return f"{instrument_type}:name:{normalized_name(name)}:"


def _classify(section: str | None, subsection: str | None, name: str) -> tuple[str, str]:
    combined = normalized_name(" ".join(filter(None, (section, subsection, name))))
    if "net receivables" in combined or "net payable" in combined:
        return "cash_receivable", "cash_receivable"
    if "reverse repo" in combined or "treps" in combined:
        return "repo_treps", "repo_treps"
    if "mutual fund unit" in combined:
        return "mutual_fund_unit", "mutual_fund_unit"
    if "foreign securities" in combined or "overseas etf" in combined:
        return "foreign_equity", "equity"
    if "equity equity related" in combined:
        return "domestic_equity", "equity"
    if "money market instruments" in combined:
        return "money_market", "money_market"
    if "debt instruments" in combined:
        return "corporate_debt", "debt"
    return "other", "other"


def _holding(
    row: list[Any], columns: dict[str, int], sheet: str, source_row: int,
    section: str | None, subsection: str | None,
) -> Holding:
    name = clean_text(_cell(row, columns, "name"))
    value = number(_cell(row, columns, "value"))
    weight = number(_cell(row, columns, "weight"), percent=True)
    if not name or value is None or weight is None:
        raise ValidationError(f"{sheet}: row {source_row} position is missing name, market value, or weight")
    isin = clean_text(_cell(row, columns, "isin"))
    if isin:
        isin = isin.upper()
        if not ISIN_RE.fullmatch(isin):
            raise ValidationError(f"{sheet}: row {source_row} has invalid ISIN {isin!r}")
    asset_class, instrument_type = _classify(section, subsection, name)
    if not isin and asset_class not in {"repo_treps", "cash_receivable"}:
        raise ValidationError(f"{sheet}: row {source_row} has no ISIN for {asset_class} position")
    quantity = number(_cell(row, columns, "quantity"))
    if instrument_type in {"equity", "mutual_fund_unit"} and quantity is None:
        raise ValidationError(f"{sheet}: row {source_row} {instrument_type} position is missing quantity")
    return Holding(
        sheet_code=sheet,
        source_row=source_row,
        source_instrument_code=clean_text(_cell(row, columns, "code")),
        source_name=name,
        normalized_name=normalized_name(name),
        isin=isin,
        asset_class=asset_class,
        instrument_type=instrument_type,
        section=section,
        subsection=subsection,
        industry_rating=clean_text(_cell(row, columns, "industry")),
        quantity=quantity,
        market_value_lakh=value,
        weight=weight,
        ytm=number(_cell(row, columns, "ytm"), percent=True),
        ytc=number(_cell(row, columns, "ytc"), percent=True),
        identity_key=_identity(instrument_type, isin, name),
    )


def _mismatch(
    issues: list[IngestionIssue], code: str, label: str, parsed: float,
    reported: float, sheet: str, *, weight: bool = False,
) -> None:
    tolerance = 0.02 if weight else max(1.0, abs(reported) * 0.0001)
    if abs(parsed - reported) > tolerance:
        issues.append(IngestionIssue(
            "warning", code,
            f"Parsed {label} {parsed:.6f} differs from reported {reported:.6f}", sheet,
        ))


def parse_sheet(sheet: str, frame: pl.DataFrame) -> FundSnapshot:
    if sheet not in FUND_NAMES:
        raise ValidationError(f"{sheet}: unrecognized TRUSTMF equity fund sheet")
    header_index, columns = _find_header(frame)
    fund_name = _scheme_name(frame, sheet)
    report_date = _report_date(frame, sheet)
    holdings: list[Holding] = []
    issues: list[IngestionIssue] = []
    section: str | None = None
    subsection: str | None = None
    grand_total: tuple[float, float] | None = None
    section_labels = {
        "equity equity related", "debt instruments", "money market instruments", "others",
        "cblo reverse repo treps",
    }
    for index in range(header_index + 1, frame.height):
        row = _row(frame, index)
        source_row = int(frame["source_row"][index])
        label = clean_text(_cell(row, columns, "name"))
        normalized = normalized_name(label or "")
        value = number(_cell(row, columns, "value"))
        weight = number(_cell(row, columns, "weight"), percent=True)
        if normalized == "grand total":
            if value is None or weight is None:
                raise ValidationError(f"{sheet}: invalid grand total")
            grand_total = (value, weight)
            break
        if normalized in section_labels:
            section, subsection = label, None
            continue
        if normalized == "net receivables payables":
            holdings.append(_holding(row, columns, sheet, source_row, label, None))
            continue
        if normalized in {"total", "sub total"} or normalized.startswith("sub total"):
            continue
        isin = clean_text(_cell(row, columns, "isin"))
        if isin or (section and "treps" in section.casefold() and value is not None and weight is not None):
            holding = _holding(row, columns, sheet, source_row, section, subsection)
            holdings.append(holding)
            if holding.asset_class == "other":
                issues.append(IngestionIssue(
                    "warning", "unknown_asset_class", "Position retained as other",
                    sheet, source_row, label,
                ))
            continue
        if not label:
            continue
        joined = " ".join(filter(None, (clean_text(value) for value in row))).casefold()
        if "nil" not in joined and len(label) < 120:
            subsection = label
    if not holdings:
        raise ValidationError(f"{sheet}: no portfolio positions detected")
    if grand_total is None:
        raise ValidationError(f"{sheet}: missing grand total")
    identities = [holding.identity_key for holding in holdings]
    duplicates = sorted({identity for identity in identities if identities.count(identity) > 1})
    if duplicates:
        raise ValidationError(f"{sheet}: duplicate holding identities: {', '.join(duplicates)}")
    _mismatch(
        issues, "grand_total_value_mismatch", "grand-total value",
        sum(holding.market_value_lakh for holding in holdings), grand_total[0], sheet,
    )
    _mismatch(
        issues, "grand_total_weight_mismatch", "grand-total weight",
        sum(holding.weight for holding in holdings), grand_total[1], sheet, weight=True,
    )
    if abs(grand_total[1] - 1.0) > 0.02:
        issues.append(IngestionIssue(
            "warning", "grand_total_weight",
            f"Reported grand-total weight is {grand_total[1]}", sheet,
        ))
    return FundSnapshot(
        sheet, fund_name, report_date, holdings, issues, grand_total[0], grand_total[1],
    )


def parse_raw_workbook(path: str | Path, raw: RawWorkbook) -> ParsedWorkbook:
    source = Path(path)
    recognized = _equity_sheets(raw)
    if not recognized:
        raise ValidationError(f"{source.name}: no recognized TRUSTMF equity fund sheets")
    snapshots = [parse_sheet(sheet, frame) for sheet, frame in recognized]
    dates = {snapshot.report_date for snapshot in snapshots}
    if len(dates) != 1:
        raise ValidationError(f"{source.name}: equity fund sheets contain inconsistent reporting dates")
    report_date = next(iter(dates))
    issues: list[IngestionIssue] = []
    missing = SHEET_CODES - {snapshot.sheet_code for snapshot in snapshots}
    if missing:
        issues.append(IngestionIssue(
            "warning", "missing_fund_sheets",
            f"Missing expected TRUSTMF equity sheets: {', '.join(sorted(missing))}",
        ))
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    return ParsedWorkbook(
        source, digest, source.stat().st_size, raw.reader, report_date, snapshots, issues,
        amc_slug=AMC_SLUG, amc_name=AMC_NAME, parser_version=PARSER_VERSION,
    )


def parse_workbook(path: str | Path) -> ParsedWorkbook:
    source = Path(path)
    return parse_raw_workbook(source, read_workbook(source))
