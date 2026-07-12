from __future__ import annotations

import calendar
import hashlib
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import polars as pl

from ..domain import FundSnapshot, Holding, IngestionIssue, MetadataOverrides, ParsedWorkbook
from ..errors import ValidationError
from ..workbooks import RawWorkbook, read_workbook

PARSER_VERSION = "oldbridge-v1"
AMC_SLUG = "oldbridge"
AMC_NAME = "Old Bridge Mutual Fund"
FUND_NAMES = {
    "OBFCE": "Old Bridge Focused Fund",
    "OBFLX": "Old Bridge Flexi Cap Fund",
}
SHEET_CODES = set(FUND_NAMES)
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
REPORT_DATE_RE = re.compile(r"monthly portfolio statement as on\s*:?[ ]*(.+)$", re.I)
FILENAME_MONTH_RE = re.compile(
    r"(?:^|[_ -])(?P<month>January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
    r"[_ -]+(?P<year>20\d{2}|\d{2})(?:[_ -]|$)", re.I,
)


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
    return parsed / 100.0 if percent else parsed


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
        "old bridge mutual fund" in text
        and "monthly portfolio statement as on" in text
        and ("old bridge focused fund" in normalized_name(text) or "old bridge flexi cap fund" in normalized_name(text))
        and "name of the instrument" in text
        and "isin" in text
    )


def matches_workbook(raw: RawWorkbook) -> bool:
    return any(_has_markers(frame) for frame in raw.sheets.values())


def _date_value(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = clean_text(value)
    if not text:
        return None
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text, flags=re.I)
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


def _filename_date(path: Path) -> date | None:
    match = FILENAME_MONTH_RE.search(path.stem)
    if not match:
        return None
    year = int(match.group("year"))
    if year < 100:
        year += 2000
    try:
        month = datetime.strptime(match.group("month")[:3], "%b").month
    except ValueError:
        return None
    return date(year, month, calendar.monthrange(year, month)[1])


def _scheme(frame: pl.DataFrame, sheet: str) -> tuple[str, str]:
    for index in range(min(frame.height, 12)):
        for value in _row(frame, index):
            text = clean_text(value)
            normalized = normalized_name(text or "")
            if "old bridge focused fund" in normalized:
                return "OBFCE", FUND_NAMES["OBFCE"]
            if "old bridge flexi cap fund" in normalized:
                return "OBFLX", FUND_NAMES["OBFLX"]
    raise ValidationError(f"{sheet}: missing or unrecognized Old Bridge scheme name")


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
            elif label.startswith("industry"):
                columns["industry"] = cell_index
            elif label == "quantity":
                columns["quantity"] = cell_index
            elif "market fair value" in label and "lakh" in label:
                columns["value"] = cell_index
            elif "net assets" in label:
                columns["weight"] = cell_index
            elif label.startswith("ytm"):
                columns["ytm"] = cell_index
        required = {"name", "isin", "industry", "quantity", "value", "weight"}
        if required <= columns.keys():
            if columns["name"] > 0:
                columns["code"] = columns["name"] - 1
            return index, columns
    raise ValidationError("missing Old Bridge portfolio header")


def _cell(row: list[Any], columns: dict[str, int], key: str) -> Any:
    index = columns.get(key, -1)
    return row[index] if 0 <= index < len(row) else None


def _identity(instrument_type: str, isin: str | None, name: str) -> str:
    if isin:
        return f"{instrument_type}:isin:{isin}"
    return f"{instrument_type}:name:{normalized_name(name)}:"


def _holding(row: list[Any], columns: dict[str, int], sheet: str, source_row: int,
             *, balance_type: str | None = None) -> Holding:
    name = clean_text(_cell(row, columns, "name"))
    value = number(_cell(row, columns, "value"))
    weight = number(_cell(row, columns, "weight"), percent=True)
    if not name or value is None or weight is None:
        raise ValidationError(f"{sheet}: row {source_row} position is missing name, market value, or weight")
    isin = clean_text(_cell(row, columns, "isin"))
    quantity = number(_cell(row, columns, "quantity"))
    if balance_type is None:
        if not isin or not ISIN_RE.fullmatch(isin.upper()):
            raise ValidationError(f"{sheet}: row {source_row} has invalid ISIN {isin!r}")
        if quantity is None:
            raise ValidationError(f"{sheet}: row {source_row} equity position is missing quantity")
        isin = isin.upper()
        asset_class, instrument_type = "domestic_equity", "equity"
        section = "Equity & Equity related"
        subsection = None
    elif balance_type == "treps":
        isin = None
        asset_class = instrument_type = "repo_treps"
        section, subsection = "Money Market Instruments", "TREPS / Reverse Repo"
    else:
        isin = None
        asset_class = instrument_type = "cash_receivable"
        section, subsection = "Other", "Net Receivables / (Payables)"
    return Holding(
        sheet_code=sheet, source_row=source_row,
        source_instrument_code=clean_text(_cell(row, columns, "code")),
        source_name=name, normalized_name=normalized_name(name), isin=isin,
        asset_class=asset_class, instrument_type=instrument_type,
        section=section, subsection=subsection,
        industry_rating=clean_text(_cell(row, columns, "industry")),
        quantity=quantity,
        market_value_lakh=value, weight=weight,
        ytm=number(_cell(row, columns, "ytm")),
        identity_key=_identity(instrument_type, isin, name),
    )


def _mismatch(issues: list[IngestionIssue], code: str, label: str, parsed: float,
              reported: float, sheet: str, *, weight: bool = False) -> None:
    tolerance = 0.02 if weight else max(1.0, abs(reported) * 0.0001)
    if abs(parsed - reported) > tolerance:
        issues.append(IngestionIssue(
            "warning", code,
            f"Parsed {label} {parsed:.6f} differs from reported {reported:.6f}", sheet,
        ))


def parse_sheet(sheet_name: str, frame: pl.DataFrame, *, fund_code_override: str | None = None) -> FundSnapshot:
    header_index, columns = _find_header(frame)
    scheme_code, fund_name = _scheme(frame, sheet_name)
    normalized_sheet = sheet_name.strip().upper()
    if normalized_sheet in SHEET_CODES and normalized_sheet != scheme_code:
        resolved = fund_code_override.strip().upper() if fund_code_override else None
        if resolved not in {normalized_sheet, scheme_code}:
            raise ValidationError(
                f"{sheet_name}: sheet code {normalized_sheet} conflicts with scheme {fund_name}"
            )
    report_date = _report_date(frame, sheet_name)
    holdings: list[Holding] = []
    issues: list[IngestionIssue] = []
    mode = "equity"
    equity_total: tuple[float, float] | None = None
    treps_total: tuple[float, float] | None = None
    grand_total: tuple[float, float] | None = None
    for index in range(header_index + 1, frame.height):
        row = _row(frame, index)
        source_row = int(frame["source_row"][index])
        label = clean_text(_cell(row, columns, "name"))
        normalized = normalized_name(label or "")
        value = number(_cell(row, columns, "value"))
        weight = number(_cell(row, columns, "weight"), percent=True)
        if normalized == "grand total":
            if value is None or weight is None:
                raise ValidationError(f"{sheet_name}: invalid grand total")
            grand_total = (value, weight)
            break
        if normalized == "money market instruments":
            mode = "money_market"
            continue
        if normalized == "treps reverse repo":
            mode = "treps"
            continue
        if normalized == "net receivables payables":
            holdings.append(_holding(row, columns, scheme_code, source_row, balance_type="cash"))
            continue
        if normalized == "total" and value is not None and weight is not None:
            if mode == "equity":
                equity_total = (value, weight)
            elif mode == "treps":
                treps_total = (value, weight)
            continue
        if normalized in {"sub total", "equity equity related", "a listed awaiting listing on stock exchanges",
                          "b unlisted"} or normalized.startswith("notes"):
            continue
        isin = clean_text(_cell(row, columns, "isin"))
        has_numbers = value is not None or weight is not None
        if mode == "treps" and has_numbers and normalized:
            holdings.append(_holding(row, columns, scheme_code, source_row, balance_type="treps"))
        elif mode == "equity" and (isin or has_numbers):
            holdings.append(_holding(row, columns, scheme_code, source_row))
    if not holdings:
        raise ValidationError(f"{sheet_name}: no portfolio positions detected")
    if grand_total is None:
        raise ValidationError(f"{sheet_name}: missing grand total")
    if equity_total is None:
        raise ValidationError(f"{sheet_name}: missing equity section total")
    identities = [holding.identity_key for holding in holdings]
    duplicates = sorted({identity for identity in identities if identities.count(identity) > 1})
    if duplicates:
        raise ValidationError(f"{sheet_name}: duplicate holding identities: {', '.join(duplicates)}")
    equity = [holding for holding in holdings if holding.asset_class == "domestic_equity"]
    treps = [holding for holding in holdings if holding.asset_class == "repo_treps"]
    _mismatch(issues, "equity_total_value_mismatch", "equity value", sum(h.market_value_lakh for h in equity), equity_total[0], scheme_code)
    _mismatch(issues, "equity_total_weight_mismatch", "equity weight", sum(h.weight for h in equity), equity_total[1], scheme_code, weight=True)
    if treps_total:
        _mismatch(issues, "treps_total_value_mismatch", "TREPS value", sum(h.market_value_lakh for h in treps), treps_total[0], scheme_code)
        _mismatch(issues, "treps_total_weight_mismatch", "TREPS weight", sum(h.weight for h in treps), treps_total[1], scheme_code, weight=True)
    _mismatch(issues, "grand_total_value_mismatch", "grand-total value", sum(h.market_value_lakh for h in holdings), grand_total[0], scheme_code)
    _mismatch(issues, "grand_total_weight_mismatch", "grand-total weight", sum(h.weight for h in holdings), grand_total[1], scheme_code, weight=True)
    if abs(grand_total[1] - 1.0) > 0.02:
        issues.append(IngestionIssue("warning", "grand_total_weight", f"Reported grand-total weight is {grand_total[1]}", scheme_code))
    return FundSnapshot(scheme_code, fund_name, report_date, holdings, issues, grand_total[0], grand_total[1])


def parse_raw_workbook(
    path: str | Path, raw: RawWorkbook, metadata: MetadataOverrides | None = None
) -> ParsedWorkbook:
    source = Path(path)
    recognized = [(name, frame) for name, frame in raw.sheets.items() if _has_markers(frame)]
    if len(recognized) != 1:
        raise ValidationError(f"{source.name}: expected exactly one Old Bridge portfolio sheet")
    sheet, frame = recognized[0]
    snapshot = parse_sheet(sheet, frame, fund_code_override=metadata.fund_code if metadata else None)
    issues: list[IngestionIssue] = []
    filename_date = _filename_date(source)
    if filename_date and filename_date != snapshot.report_date:
        issues.append(IngestionIssue(
            "warning", "filename_date_mismatch",
            f"Filename date {filename_date} differs from workbook date {snapshot.report_date}",
        ))
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    return ParsedWorkbook(
        source, digest, source.stat().st_size, raw.reader, snapshot.report_date, [snapshot], issues,
        amc_slug=AMC_SLUG, amc_name=AMC_NAME, parser_version=PARSER_VERSION,
    )


def parse_workbook(path: str | Path) -> ParsedWorkbook:
    source = Path(path)
    return parse_raw_workbook(source, read_workbook(source))
