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

PARSER_VERSION = "helios-v1"
AMC_SLUG = "helios"
AMC_NAME = "Helios Mutual Fund"
FUND_NAMES = {
    "HFCF": "Helios Flexi Cap Fund",
    "HMCF": "Helios Mid Cap Fund",
    "HSCF": "Helios Small Cap Fund",
    "HFSF": "Helios Financial Services Fund",
}
SHEET_CODES = set(FUND_NAMES)
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
FILENAME_DATE_RE = re.compile(
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?[-_ ]+(?P<month>[A-Za-z]+)[-_ ]+(?P<year>\d{4})",
    re.I,
)


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()
    return text or None


def normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = clean_text(value)
    if text is None or text.upper() in {"NIL", "NA", "N/A", "-"}:
        return None
    text = text.replace(",", "").replace("₹", "").replace("$", "").strip()
    if text.endswith("%"):
        text = text[:-1].strip()
    try:
        return float(text)
    except ValueError:
        return None


def percentage_points(value: Any) -> float | None:
    parsed = number(value)
    return None if parsed is None else parsed / 100.0


def _row(frame: pl.DataFrame, index: int) -> list[Any]:
    columns = [name for name in frame.columns if name.startswith("column_")]
    values = frame.row(index, named=True)
    return [values.get(name) for name in columns]


def _row_text(row: list[Any]) -> str:
    return " ".join(filter(None, (clean_text(value) for value in row)))


def _filename_date(path: Path) -> date | None:
    match = FILENAME_DATE_RE.search(path.stem)
    if not match:
        return None
    try:
        return datetime.strptime(
            f"{match.group('day')} {match.group('month')} {match.group('year')}", "%d %B %Y"
        ).date()
    except ValueError:
        return None


def _date_value(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = clean_text(value)
    if not text:
        return None
    text = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", text, flags=re.I)
    for fmt in ("%d %B %Y", "%B %d %Y", "%d-%B-%Y", "%d/%m/%Y", "%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _report_date(frame: pl.DataFrame, sheet: str) -> date:
    for index in range(min(frame.height, 20)):
        row = _row(frame, index)
        for cell_index, value in enumerate(row):
            text = clean_text(value)
            if text and "portfolio statement as on" in text.casefold():
                for candidate in row[cell_index + 1 :]:
                    parsed = _date_value(candidate)
                    if parsed:
                        return parsed
                suffix = re.split(r"portfolio statement as on\s*:?", text, flags=re.I)[-1]
                parsed = _date_value(suffix)
                if parsed:
                    return parsed
                raise ValidationError(f"{sheet}: invalid portfolio reporting date")
    raise ValidationError(f"{sheet}: missing portfolio reporting date")


def _find_header(frame: pl.DataFrame) -> tuple[int, dict[str, int]]:
    for index in range(min(frame.height, 30)):
        row = _row(frame, index)
        normalized = [normalized_name(clean_text(value) or "") for value in row]
        columns: dict[str, int] = {}
        for cell_index, label in enumerate(normalized):
            if "name of the instrument" in label:
                columns["name"] = cell_index
            elif label == "isin":
                columns["isin"] = cell_index
            elif "rating industry" in label:
                columns["industry"] = cell_index
            elif label == "quantity":
                columns["quantity"] = cell_index
            elif "market value" in label and "lakh" in label:
                columns["value"] = cell_index
            elif "aum" in label and ("percent" in label or label.startswith("to aum")):
                columns["weight"] = cell_index
            elif label.startswith("ytm"):
                columns["ytm"] = cell_index
            elif label.startswith("ytc"):
                columns["ytc"] = cell_index
        required = {"name", "isin", "industry", "quantity", "value", "weight"}
        if required <= columns.keys():
            columns["code"] = columns["name"] - 1
            return index, columns
    raise ValidationError("missing Helios portfolio header")


def _scheme_name(frame: pl.DataFrame, sheet: str) -> str:
    for index in range(min(frame.height, 15)):
        row = _row(frame, index)
        for cell_index, value in enumerate(row):
            text = clean_text(value)
            if text and normalized_name(text).startswith("scheme name"):
                scheme = next((clean_text(v) for v in row[cell_index + 1 :] if clean_text(v)), None)
                if not scheme:
                    break
                expected = normalized_name(FUND_NAMES[sheet])
                if expected not in normalized_name(scheme):
                    raise ValidationError(
                        f"{sheet}: scheme name {scheme!r} does not match {FUND_NAMES[sheet]!r}"
                    )
                return scheme
    raise ValidationError(f"{sheet}: missing scheme name")


def _has_helios_markers(frame: pl.DataFrame) -> bool:
    text = " ".join(_row_text(_row(frame, index)) for index in range(min(frame.height, 12))).casefold()
    return "helios mutual fund" in text and "portfolio statement as on" in text and "name of the instrument" in text


def matches_workbook(raw: RawWorkbook) -> bool:
    recognized = [(name.strip().upper(), frame) for name, frame in raw.sheets.items() if name.strip().upper() in SHEET_CODES]
    return any(_has_helios_markers(frame) for _, frame in recognized)


def _identity(instrument_type: str, isin: str | None, name: str) -> str:
    if isin:
        return f"{instrument_type}:isin:{isin}"
    return f"{instrument_type}:name:{normalized_name(name)}:"


def _cell(row: list[Any], columns: dict[str, int], key: str) -> Any:
    index = columns.get(key, -1)
    return row[index] if 0 <= index < len(row) else None


def _position(
    row: list[Any], columns: dict[str, int], sheet: str, source_row: int, *, balance_type: str | None = None
) -> Holding:
    name = clean_text(_cell(row, columns, "name"))
    value = number(_cell(row, columns, "value"))
    weight = percentage_points(_cell(row, columns, "weight"))
    if not name or value is None or weight is None:
        raise ValidationError(f"{sheet}: row {source_row} position is missing name, market value, or weight")
    isin = clean_text(_cell(row, columns, "isin"))
    if balance_type is None:
        if not isin or not ISIN_RE.fullmatch(isin.upper()):
            raise ValidationError(f"{sheet}: row {source_row} has invalid ISIN {isin!r}")
        isin = isin.upper()
        asset_class, instrument_type = "domestic_equity", "equity"
        section, subsection = "EQUITY & EQUITY RELATED", "Listed/awaiting listing on Stock Exchanges"
    elif balance_type == "treps":
        isin = None
        asset_class = instrument_type = "repo_treps"
        section, subsection = "OTHERS", "TREPS / Reverse Repo Investments"
    else:
        isin = None
        asset_class = instrument_type = "cash_receivable"
        section, subsection = "OTHERS", "Other Current Assets / (Liabilities)"
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
        quantity=number(_cell(row, columns, "quantity")),
        market_value_lakh=value,
        weight=weight,
        ytm=percentage_points(_cell(row, columns, "ytm")),
        ytc=percentage_points(_cell(row, columns, "ytc")),
        identity_key=_identity(instrument_type, isin, name),
    )


def _mismatch_issue(
    issues: list[IngestionIssue], code: str, label: str, parsed: float, reported: float, sheet: str, *, weight: bool = False
) -> None:
    tolerance = 0.02 if weight else max(1.0, abs(reported) * 0.0001)
    if abs(parsed - reported) > tolerance:
        issues.append(IngestionIssue(
            "warning", code, f"Parsed {label} {parsed:.6f} differs from reported {reported:.6f}", sheet
        ))


def parse_sheet(sheet: str, frame: pl.DataFrame) -> FundSnapshot:
    if sheet not in FUND_NAMES:
        raise ValidationError(f"{sheet}: unrecognized Helios fund sheet")
    header_index, columns = _find_header(frame)
    _scheme_name(frame, sheet)
    report_date = _report_date(frame, sheet)
    holdings: list[Holding] = []
    issues: list[IngestionIssue] = []
    in_equity = False
    before_notes = True
    equity_value: float | None = None
    equity_weight: float | None = None
    grand_value: float | None = None
    grand_weight: float | None = None
    known_labels = {
        "equity equity related", "listed awaiting listing on stock exchanges", "others",
        "treps reverse repo investments", "other current assets liabilities", "total", "grand total aum",
    }
    for index in range(header_index + 1, frame.height):
        row = _row(frame, index)
        source_row = int(frame["source_row"][index])
        label = clean_text(_cell(row, columns, "name"))
        if not label:
            isin = clean_text(_cell(row, columns, "isin"))
            has_position_numbers = (
                number(_cell(row, columns, "value")) is not None
                or number(_cell(row, columns, "weight")) is not None
            )
            if in_equity and (isin or has_position_numbers):
                _position(row, columns, sheet, source_row)
            continue
        normalized = normalized_name(label)
        if normalized.startswith("notes symbols"):
            before_notes = False
            break
        if normalized == "equity equity related":
            in_equity = True
            continue
        if normalized == "others":
            in_equity = False
            continue
        if normalized == "total":
            value = number(_cell(row, columns, "value"))
            weight = percentage_points(_cell(row, columns, "weight"))
            if in_equity and value is not None and weight is not None:
                equity_value, equity_weight = value, weight
                in_equity = False
            continue
        if normalized == "grand total aum":
            grand_value = number(_cell(row, columns, "value"))
            grand_weight = percentage_points(_cell(row, columns, "weight"))
            continue
        if normalized == "treps":
            holdings.append(_position(row, columns, sheet, source_row, balance_type="treps"))
            continue
        if normalized == "net receivable payable":
            holdings.append(_position(row, columns, sheet, source_row, balance_type="cash"))
            continue
        isin = clean_text(_cell(row, columns, "isin"))
        has_position_numbers = number(_cell(row, columns, "value")) is not None or number(_cell(row, columns, "weight")) is not None
        if in_equity and (isin or has_position_numbers):
            holdings.append(_position(row, columns, sheet, source_row))
            continue
        if before_notes and normalized not in known_labels:
            issues.append(IngestionIssue(
                "warning", "unknown_portfolio_row", "Unrecognized row retained outside holdings", sheet, source_row, label
            ))
    if not holdings:
        raise ValidationError(f"{sheet}: no portfolio positions detected")
    identities = [holding.identity_key for holding in holdings]
    duplicates = sorted({identity for identity in identities if identities.count(identity) > 1})
    if duplicates:
        raise ValidationError(f"{sheet}: duplicate holding identities: {', '.join(duplicates)}")
    if equity_value is None or equity_weight is None:
        raise ValidationError(f"{sheet}: missing equity section total")
    if grand_value is None or grand_weight is None:
        raise ValidationError(f"{sheet}: missing grand total AUM")
    equity = [holding for holding in holdings if holding.asset_class == "domestic_equity"]
    _mismatch_issue(issues, "equity_total_value_mismatch", "equity value", sum(h.market_value_lakh for h in equity), equity_value, sheet)
    _mismatch_issue(issues, "equity_total_weight_mismatch", "equity weight", sum(h.weight for h in equity), equity_weight, sheet, weight=True)
    _mismatch_issue(issues, "grand_total_value_mismatch", "grand-total value", sum(h.market_value_lakh for h in holdings), grand_value, sheet)
    _mismatch_issue(issues, "grand_total_weight_mismatch", "grand-total weight", sum(h.weight for h in holdings), grand_weight, sheet, weight=True)
    if abs(grand_weight - 1.0) > 0.02:
        issues.append(IngestionIssue("warning", "grand_total_weight", f"Reported grand-total weight is {grand_weight}", sheet))
    return FundSnapshot(sheet, FUND_NAMES[sheet], report_date, holdings, issues, grand_value, grand_weight)


def parse_raw_workbook(path: str | Path, raw: RawWorkbook) -> ParsedWorkbook:
    source = Path(path)
    recognized = [(name.strip().upper(), frame) for name, frame in raw.sheets.items() if name.strip().upper() in SHEET_CODES]
    if len(recognized) != 1:
        raise ValidationError(f"{source.name}: expected exactly one recognized Helios fund sheet")
    sheet, frame = recognized[0]
    if not _has_helios_markers(frame):
        raise ValidationError(f"{source.name}: recognized sheet lacks Helios portfolio markers")
    snapshot = parse_sheet(sheet, frame)
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
