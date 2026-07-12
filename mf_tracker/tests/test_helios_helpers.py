from datetime import date, datetime
from pathlib import Path

import pytest

from mf_tracker.amcs.helios import (
    FUND_NAMES,
    _date_value,
    _filename_date,
    _find_header,
    _identity,
    clean_text,
    number,
    parse_sheet,
    percentage_points,
)
from mf_tracker.errors import ValidationError
from mf_tracker.workbooks import _rows_to_frame


def workbook_rows(*positions: tuple[str | None, str, str, float, float]):
    rows = [
        [None] * 11,
        [None, None, "Helios Mutual Fund"] + [None] * 8,
        [None, None, "SCHEME NAME :", "Helios Flexi Cap Fund (An open-ended equity scheme)"] + [None] * 7,
        [None, None, "PORTFOLIO STATEMENT AS ON :", datetime(2026, 6, 30)] + [None] * 7,
        [None] * 11,
        [None, None, "Name of the Instrument / Issuer", "ISIN", "Rating / Industry^", "Quantity",
         "Market value\n(Rs. in Lakhs)", "% to AUM", "YTM %", "YTC % ##", "Notes & Symbols"],
        [None] * 11,
        [None, None, "EQUITY & EQUITY RELATED"] + [None] * 8,
        [None, None, "Listed/awaiting listing on Stock Exchanges"] + [None] * 8,
    ]
    for code, name, isin, value, weight in positions:
        rows.append([None, code, name, isin, "Banks", 10, value, weight, None, None, None])
    equity_value = sum(position[3] for position in positions)
    equity_weight = sum(position[4] for position in positions)
    rows.extend([
        [None, None, "Total", None, None, None, equity_value, equity_weight, None, None, None],
        [None, None, "OTHERS"] + [None] * 8,
        [None, None, "TREPS / Reverse Repo Investments"] + [None] * 8,
        [None, None, "TREPS", None, None, None, 5.0, 5.0, None, None, None],
        [None, None, "Total", None, None, None, 5.0, 5.0, None, None, None],
        [None, None, "Other Current Assets / (Liabilities)"] + [None] * 8,
        [None, None, "Net Receivable / Payable", None, None, None, -1.0, -1.0, None, None, None],
        [None, None, "Total", None, None, None, -1.0, -1.0, None, None, None],
        [None, None, "GRAND TOTAL (AUM)", None, None, None, equity_value + 4.0, equity_weight + 4.0],
        [None, None, "Notes & Symbols :-"] + [None] * 8,
    ])
    return rows


def test_normalizers_dates_and_identity():
    assert clean_text(" A\xa0  B\n") == "A B"
    assert number("1,234.50") == 1234.5
    assert percentage_points(-0.31) == pytest.approx(-0.0031)
    assert _date_value(datetime(2026, 6, 30)) == date(2026, 6, 30)
    assert _date_value("30th June 2026") == date(2026, 6, 30)
    assert _filename_date(Path("Helios-Flexi-Cap-Fund-28th-February-2026.xlsx")) == date(2026, 2, 28)
    assert _filename_date(Path("Helios-Flexi-Cap-Fund-Monthly-Portfolio-as-on-30th-June-2026.xlsx")) == date(2026, 6, 30)
    assert _identity("equity", "INE040A01034", "HDFC Bank") == "equity:isin:INE040A01034"


def test_header_and_all_fund_mappings():
    frame = _rows_to_frame(workbook_rows((None, "HDFC Bank Ltd.", "INE040A01034", 96.0, 96.0)))
    header, columns = _find_header(frame)
    assert header == 5
    assert columns["name"] == 2
    assert FUND_NAMES == {
        "HFCF": "Helios Flexi Cap Fund",
        "HMCF": "Helios Mid Cap Fund",
        "HSCF": "Helios Small Cap Fund",
        "HFSF": "Helios Financial Services Fund",
    }


def test_parse_sheet_handles_missing_code_and_negative_payable():
    frame = _rows_to_frame(workbook_rows((None, "HDFC Bank Ltd.", "INE040A01034", 96.0, 96.0)))
    snapshot = parse_sheet("HFCF", frame)
    assert snapshot.fund_name == "Helios Flexi Cap Fund"
    assert len(snapshot.holdings) == 3
    assert snapshot.holdings[0].source_instrument_code is None
    payable = next(h for h in snapshot.holdings if h.asset_class == "cash_receivable")
    assert payable.market_value_lakh == -1.0
    assert payable.weight == pytest.approx(-0.01)
    assert snapshot.reported_total_weight == 1.0


def test_malformed_isin_duplicate_and_missing_header_are_strict():
    malformed = _rows_to_frame(workbook_rows(("1", "Bad", "INVALID", 96.0, 96.0)))
    with pytest.raises(ValidationError, match="invalid ISIN"):
        parse_sheet("HFCF", malformed)
    duplicate = _rows_to_frame(workbook_rows(
        ("1", "HDFC Bank", "INE040A01034", 48.0, 48.0),
        ("2", "HDFC Bank", "INE040A01034", 48.0, 48.0),
    ))
    with pytest.raises(ValidationError, match="duplicate holding identities"):
        parse_sheet("HFCF", duplicate)
    missing_name_rows = workbook_rows(("1", "HDFC Bank", "INE040A01034", 96.0, 96.0))
    missing_name_rows[9][2] = None
    with pytest.raises(ValidationError, match="missing name"):
        parse_sheet("HFCF", _rows_to_frame(missing_name_rows))
    with pytest.raises(ValidationError, match="missing Helios portfolio header"):
        parse_sheet("HFCF", _rows_to_frame([["Helios Mutual Fund"]]))


def test_reconciliation_discrepancies_are_warnings():
    rows = workbook_rows(("1", "HDFC Bank", "INE040A01034", 96.0, 96.0))
    rows[-2][6] = 90.0
    snapshot = parse_sheet("HFCF", _rows_to_frame(rows))
    assert {issue.code for issue in snapshot.issues} == {"grand_total_value_mismatch"}
