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

PARSER_VERSION = "ppfas-v1"
SHEET_CODES = {"PPFCF", "PPLF", "PPTSF", "PPCHF", "PPAF", "PPDAAF", "PPLCF"}
EXPECTED_SHEETS = SHEET_CODES
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")
DATE_RE = re.compile(r"Monthly Portfolio Statement as on\s+([A-Za-z]+ \d{1,2}, \d{4})", re.I)
FILENAME_DATE_RE = re.compile(r"([A-Za-z]+)_([0-9]{1,2})_([0-9]{4})", re.I)
EXPIRY_RE = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})\s+Future\b", re.I)

EQUITY_SECTION = "equity & equity related"


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value).replace("\xa0", " ")).strip()
    return text or None


def normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def number(value: Any, *, percent: bool = False) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = clean_text(value)
    if text is None or text.upper() in {"NIL", "NA", "N/A", "-"}:
        return None
    text = text.replace(",", "").replace("₹", "").replace("$", "").strip()
    is_percent = text.endswith("%")
    if is_percent:
        text = text[:-1]
    try:
        parsed = float(text)
    except ValueError:
        return None
    return parsed / 100.0 if is_percent or (percent and abs(parsed) > 1.0) else parsed


def _row(frame: pl.DataFrame, index: int) -> list[Any]:
    columns = [name for name in frame.columns if name.startswith("column_")]
    values = frame.row(index, named=True)
    return [values.get(name) for name in columns]


def _first_text(row: list[Any]) -> str | None:
    for value in row:
        text = clean_text(value)
        if text:
            return text
    return None


def _report_date(frame: pl.DataFrame, sheet: str) -> date:
    for index in range(min(frame.height, 15)):
        joined = " ".join(filter(None, (clean_text(value) for value in _row(frame, index))))
        match = DATE_RE.search(joined)
        if match:
            return datetime.strptime(match.group(1), "%B %d, %Y").date()
    raise ValidationError(f"{sheet}: missing monthly portfolio reporting date")


def _filename_date(path: Path) -> date | None:
    match = FILENAME_DATE_RE.search(path.stem)
    if not match:
        return None
    try:
        return datetime.strptime(" ".join(match.groups()), "%B %d %Y").date()
    except ValueError:
        return None


def _find_main_header(frame: pl.DataFrame) -> int:
    for index in range(min(frame.height, 30)):
        joined = " | ".join(filter(None, (clean_text(value) for value in _row(frame, index)))).casefold()
        if "name of the instrument" in joined and "isin" in joined:
            return index
    return -1


def _find_isin(row: list[Any]) -> tuple[str | None, int | None]:
    for index, value in enumerate(row[:10]):
        text = clean_text(value)
        if text and ISIN_RE.fullmatch(text.upper()):
            return text.upper(), index
    return None, None


def _is_total(label: str) -> bool:
    lowered = label.casefold()
    return lowered in {"total", "sub total", "grand total"} or lowered.startswith("sub total ")


def _classify(section: str | None, subsection: str | None, name: str, *, derivative: bool = False) -> tuple[str, str]:
    combined = " ".join(filter(None, [section, subsection, name])).casefold()
    if derivative:
        # The shared source subsection is "Index / Stock Futures"; classify
        # from the contract name rather than that label.
        derivative_name = name.casefold()
        if "nifty" in derivative_name or "sensex" in derivative_name or " index " in f" {derivative_name} ":
            return "index_future", "index_future"
        return "equity_future", "equity_future"
    if "net receivables" in combined or "net payable" in combined:
        return "cash_receivable", "cash_receivable"
    if "reverse repo" in combined or "treps" in combined:
        return "repo_treps", "repo_treps"
    if "mutual fund unit" in combined:
        return "mutual_fund_unit", "mutual_fund_unit"
    if "corporate debt market development fund" in combined:
        return "mutual_fund_unit", "mutual_fund_unit"
    if "foreign" in combined or "overseas" in combined:
        return "foreign_equity", "equity"
    if EQUITY_SECTION in combined:
        return "domestic_equity", "equity"
    if any(token in combined for token in ("arbitrage", "reit", "invit", "special situation")):
        return "domestic_equity", "reit" if "reit" in combined else "equity"
    if any(token in combined for token in ("treasury bill", "certificate of deposit", "commercial paper", "money market")):
        return "money_market", "money_market"
    if any(token in combined for token in ("government securities", "government security", "state development loan")):
        return "government_security", "debt"
    if any(token in combined for token in ("debt instruments", "non convertible", "bonds", "debentures")):
        return "corporate_debt", "debt"
    return "other", "other"


def _expiry(name: str) -> str | None:
    match = EXPIRY_RE.search(name)
    if not match:
        return None
    return datetime.strptime(f"{match.group(1)} {match.group(2)}", "%B %Y").strftime("%Y-%m")


def _identity(instrument_type: str, isin: str | None, name: str, expiry: str | None) -> str:
    if isin:
        return f"{instrument_type}:isin:{isin}"
    return f"{instrument_type}:name:{normalized_name(name)}:{expiry or ''}"


def _main_position(row: list[Any], sheet: str, source_row: int, section: str | None, subsection: str | None) -> Holding | None:
    isin, isin_index = _find_isin(row)
    label = _first_text(row)
    if not label or _is_total(label) or label.casefold() == "nil":
        return None
    # PPFAS main tables are positional: code, name, ISIN, class, quantity, value, weight, yields.
    if isin_index is not None:
        name_index = isin_index - 1
        code_index = isin_index - 2
        class_index = isin_index + 1
        quantity_index = isin_index + 2
        value_index = isin_index + 3
        weight_index = isin_index + 4
    elif "net receivables" in " ".join(filter(None, (clean_text(v) for v in row))).casefold():
        name_index, code_index, class_index = 1, 0, 2
        quantity_index, value_index, weight_index = 4, 5, 6
    elif "treps" in (section or "").casefold() or "reverse repo" in (section or "").casefold():
        name_index, code_index, class_index = 1, 0, 3
        quantity_index, value_index, weight_index = 4, 5, 6
    else:
        return None
    name = clean_text(row[name_index] if name_index < len(row) else None)
    if not name:
        return None
    value = number(row[value_index] if value_index < len(row) else None)
    weight = number(row[weight_index] if weight_index < len(row) else None, percent=True)
    if value is None or weight is None:
        return None
    asset_class, instrument_type = _classify(section, subsection, name)
    return Holding(
        sheet_code=sheet,
        source_row=source_row,
        source_instrument_code=clean_text(row[code_index] if 0 <= code_index < len(row) else None),
        source_name=name,
        normalized_name=normalized_name(name),
        isin=isin,
        asset_class=asset_class,
        instrument_type=instrument_type,
        section=section,
        subsection=subsection,
        industry_rating=clean_text(row[class_index] if class_index < len(row) else None),
        quantity=number(row[quantity_index] if quantity_index < len(row) else None),
        market_value_lakh=value,
        weight=weight,
        ytm=number(row[weight_index + 1] if weight_index + 1 < len(row) else None, percent=True),
        ytc=number(row[weight_index + 2] if weight_index + 2 < len(row) else None, percent=True),
        identity_key=_identity(instrument_type, isin, name, None),
    )


def _derivative_position(row: list[Any], sheet: str, source_row: int) -> Holding | None:
    nonempty = [(index, clean_text(value)) for index, value in enumerate(row[:10]) if clean_text(value)]
    if not nonempty:
        return None
    label = nonempty[0][1] or ""
    if _is_total(label) or label.casefold() in {"index / stock futures", "name of the instrument"}:
        return None
    direction_index = next((index for index, value in nonempty if value and value.casefold() in {"long", "short", "(short)", "(long)"}), None)
    if direction_index is None:
        return None
    name_index = direction_index - 1
    name = clean_text(row[name_index])
    if not name:
        return None
    code = clean_text(row[name_index - 1]) if name_index > 0 else None
    quantity = number(row[direction_index + 1] if direction_index + 1 < len(row) else None)
    value = number(row[direction_index + 2] if direction_index + 2 < len(row) else None)
    weight = number(row[direction_index + 3] if direction_index + 3 < len(row) else None, percent=True)
    if quantity is None or value is None or weight is None:
        return None
    direction = "short" if "short" in clean_text(row[direction_index]).casefold() else "long"
    expiry = _expiry(name)
    asset_class, instrument_type = _classify("derivatives", "index / stock futures", name, derivative=True)
    return Holding(
        sheet_code=sheet,
        source_row=source_row,
        source_instrument_code=code,
        source_name=name,
        normalized_name=normalized_name(name),
        isin=None,
        asset_class=asset_class,
        instrument_type=instrument_type,
        section="Derivatives",
        subsection="Index / Stock Futures",
        industry_rating=None,
        quantity=quantity,
        market_value_lakh=value,
        weight=weight,
        direction=direction,
        expiry=expiry,
        identity_key=_identity(instrument_type, None, name, expiry),
    )


def parse_sheet(sheet: str, frame: pl.DataFrame) -> FundSnapshot:
    header_index = _find_main_header(frame)
    if header_index < 0:
        raise ValidationError(f"{sheet}: missing portfolio header")
    report_date = _report_date(frame, sheet)
    first_row = _row(frame, 0)
    fund_name = _first_text(first_row) or sheet
    holdings: list[Holding] = []
    issues: list[IngestionIssue] = []
    section: str | None = None
    subsection: str | None = None
    in_derivatives = False
    grand_value: float | None = None
    grand_weight: float | None = None
    for index in range(header_index + 1, frame.height):
        row = _row(frame, index)
        source_row = int(frame["source_row"][index])
        label = _first_text(row)
        if not label:
            continue
        lowered = label.casefold()
        if lowered == "derivatives":
            in_derivatives = True
            continue
        if in_derivatives:
            if lowered.startswith(("# traded", "$ less", "~ yield", "notes", "1.")):
                break
            holding = _derivative_position(row, sheet, source_row)
            if holding:
                holdings.append(holding)
            continue
        if lowered == "grand total":
            raw_numeric = [value for value in row if number(value) is not None]
            if raw_numeric:
                grand_value = number(raw_numeric[-2]) if len(raw_numeric) >= 2 else number(raw_numeric[-1])
                grand_weight = number(raw_numeric[-1], percent=True) if len(raw_numeric) >= 2 else None
            continue
        isin, _ = _find_isin(row)
        joined = " ".join(filter(None, (clean_text(value) for value in row))).casefold()
        position = _main_position(row, sheet, source_row, section, subsection)
        if position:
            holdings.append(position)
            if position.asset_class == "other":
                issues.append(IngestionIssue("warning", "unknown_asset_class", "Position retained as other", sheet, source_row, label))
            continue
        # Non-position text before GRAND TOTAL updates the hierarchy. Totals and NIL rows do not.
        if not isin and not _is_total(label) and "nil" not in joined:
            if lowered.startswith("(") or lowered in {"listed", "unlisted"}:
                subsection = label
            elif len(label) < 100 and not any(token in lowered for token in ("monthly portfolio", "name of the instrument")):
                if any(token in lowered for token in ("listed", "privately placed")):
                    subsection = label
                else:
                    section, subsection = label, None
    if not holdings:
        raise ValidationError(f"{sheet}: no portfolio positions detected")
    main_holdings = [holding for holding in holdings if holding.section != "Derivatives"]
    if grand_value is not None:
        parsed_value = sum(holding.market_value_lakh for holding in main_holdings)
        tolerance = max(1.0, abs(grand_value) * 0.0001)
        if abs(parsed_value - grand_value) > tolerance:
            issues.append(IngestionIssue(
                "warning", "grand_total_value_mismatch",
                f"Parsed main-portfolio value {parsed_value:.2f} differs from reported {grand_value:.2f}", sheet,
            ))
    if grand_weight is not None:
        parsed_weight = sum(holding.weight for holding in main_holdings)
        if abs(parsed_weight - grand_weight) > 0.02:
            issues.append(IngestionIssue(
                "warning", "grand_total_weight_mismatch",
                f"Parsed main-portfolio weight {parsed_weight:.6f} differs from reported {grand_weight:.6f}", sheet,
            ))
    if grand_weight is not None and abs(grand_weight - 1.0) > 0.02:
        issues.append(IngestionIssue("warning", "grand_total_weight", f"Reported grand-total weight is {grand_weight}", sheet))
    return FundSnapshot(sheet, fund_name, report_date, holdings, issues, grand_value, grand_weight)


def matches_workbook(raw: RawWorkbook) -> bool:
    return any(name.strip().upper() in SHEET_CODES for name in raw.sheets)


def parse_raw_workbook(path: str | Path, raw: RawWorkbook) -> ParsedWorkbook:
    source = Path(path)
    recognized = [(name, frame) for name, frame in raw.sheets.items() if name.strip().upper() in SHEET_CODES]
    if not recognized:
        raise ValidationError(f"{source.name}: no recognized PPFAS fund sheets")
    snapshots = [parse_sheet(name.strip().upper(), frame) for name, frame in recognized]
    dates = {snapshot.report_date for snapshot in snapshots}
    if len(dates) != 1:
        raise ValidationError(f"{source.name}: fund sheets contain inconsistent reporting dates")
    report_date = next(iter(dates))
    issues: list[IngestionIssue] = []
    filename_date = _filename_date(source)
    if filename_date and filename_date != report_date:
        issues.append(IngestionIssue("warning", "filename_date_mismatch", f"Filename date {filename_date} differs from workbook date {report_date}"))
    missing = EXPECTED_SHEETS - {snapshot.sheet_code for snapshot in snapshots}
    if missing and not (report_date.year == 2026 and report_date.month == 1 and missing == {"PPLCF"}):
        issues.append(IngestionIssue("warning", "missing_fund_sheets", f"Missing expected sheets: {', '.join(sorted(missing))}"))
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    return ParsedWorkbook(
        source, digest, source.stat().st_size, raw.reader, report_date, snapshots, issues,
        amc_slug="ppfas", amc_name="PPFAS Mutual Fund", parser_version=PARSER_VERSION,
    )


def parse_workbook(path: str | Path) -> ParsedWorkbook:
    source = Path(path)
    return parse_raw_workbook(source, read_workbook(source))
