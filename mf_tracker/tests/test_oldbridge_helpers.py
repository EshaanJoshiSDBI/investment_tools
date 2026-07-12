from datetime import date
from pathlib import Path

import pytest

from mf_tracker.amcs.oldbridge import _filename_date, matches_workbook, parse_sheet
from mf_tracker.errors import ValidationError
from mf_tracker.workbooks import RawWorkbook, _rows_to_frame


def rows(*positions):
    values = [
        ["OBFCE", "Old Bridge Mutual Fund"],
        [None, "Monthly Portfolio Statement as on June 30, 2026"],
        [None, "Old  Bridge Focused  Fund"],
        [],
        [None, "Name of the Instrument", "ISIN", "Industry*", "Quantity",
         "Market/Fair Value (Rs. in Lakhs)", "% to Net Assets", "YTM"],
        [None, "Equity & Equity related"],
        [None, "(a) Listed / awaiting listing on Stock Exchanges"],
    ]
    for code, name, isin, value, weight in positions:
        values.append([code, name, isin, "Banks", 10, value, weight, None])
    equity_value = sum(position[3] for position in positions)
    equity_weight = sum(position[4] for position in positions)
    values.extend([
        [None, "Sub Total", None, None, None, equity_value, equity_weight],
        [None, "Total", None, None, None, equity_value, equity_weight],
        [None, "Money Market Instruments"],
        [None, "TREPS / Reverse Repo"],
        ["TRP", "Triparty Repo", None, None, None, 3, 3, 0.05],
        [None, "Total", None, None, None, 3, 3],
        [None, "Net Receivables / (Payables)", None, None, None, -1, -1],
        [None, "GRAND TOTAL", None, None, None, equity_value + 2, equity_weight + 2],
        [None, "Notes :"],
        [None, "HDFC Bank Limited", "INE040A01034", "Banks", 99, 99, 99],
    ])
    return values


def test_markers_filename_dates_and_footer_stop():
    frame = _rows_to_frame(rows(("HDFB03", "HDFC Bank", "INE040A01034", 98, 98)))
    assert matches_workbook(RawWorkbook("openpyxl", {"Sheet1": frame}))
    snapshot = parse_sheet("Sheet1", frame)
    assert snapshot.report_date == date(2026, 6, 30)
    assert snapshot.sheet_code == "OBFCE"
    assert len(snapshot.holdings) == 3
    assert _filename_date(Path("Old_Bridge_Focused_Fund_Feb_26_x.xlsx")) == date(2026, 2, 28)
    assert _filename_date(Path("OBFE_x.xlsx")) is None


def test_malformed_positions_duplicates_totals_and_sheet_conflicts_are_strict():
    invalid = _rows_to_frame(rows(("BAD", "Bad", "INVALID", 98, 98)))
    with pytest.raises(ValidationError, match="invalid ISIN"):
        parse_sheet("OBFCE", invalid)
    missing_quantity = rows(("BAD", "Bad", "INE040A01034", 98, 98))
    missing_quantity[7][4] = None
    with pytest.raises(ValidationError, match="missing quantity"):
        parse_sheet("OBFCE", _rows_to_frame(missing_quantity))
    duplicate = _rows_to_frame(rows(
        ("ONE", "HDFC Bank", "INE040A01034", 49, 49),
        ("TWO", "HDFC Bank", "INE040A01034", 49, 49),
    ))
    with pytest.raises(ValidationError, match="duplicate holding identities"):
        parse_sheet("OBFCE", duplicate)
    missing_total_rows = rows(("HDFB03", "HDFC Bank", "INE040A01034", 98, 98))
    missing_total_rows = [row for row in missing_total_rows if "GRAND TOTAL" not in row]
    with pytest.raises(ValidationError, match="missing grand total"):
        parse_sheet("OBFCE", _rows_to_frame(missing_total_rows))
    with pytest.raises(ValidationError, match="conflicts with scheme"):
        parse_sheet("OBFLX", _rows_to_frame(rows(
            ("HDFB03", "HDFC Bank", "INE040A01034", 98, 98)
        )))


def test_total_discrepancies_are_warnings():
    source = rows(("HDFB03", "HDFC Bank", "INE040A01034", 98, 98))
    grand = next(row for row in source if "GRAND TOTAL" in row)
    grand[5] = 80
    snapshot = parse_sheet("OBFCE", _rows_to_frame(source))
    assert {issue.code for issue in snapshot.issues} == {"grand_total_value_mismatch"}
