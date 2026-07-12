from unittest.mock import patch

import pytest

from mf_tracker.errors import ValidationError
from mf_tracker.parsing import parse_workbook
from mf_tracker.workbooks import RawWorkbook, _rows_to_frame


def test_unknown_workbook_is_rejected(tmp_path):
    raw = RawWorkbook("openpyxl", {"Sheet1": _rows_to_frame([["unknown"]])})
    with patch("mf_tracker.parsing.read_workbook", return_value=raw):
        with pytest.raises(ValidationError, match="no recognized AMC"):
            parse_workbook(tmp_path / "unknown.xlsx")


def test_ambiguous_workbook_is_rejected(tmp_path):
    helios = _rows_to_frame([
        ["Helios Mutual Fund"],
        ["PORTFOLIO STATEMENT AS ON"],
        ["Name of the Instrument"],
    ])
    raw = RawWorkbook("openpyxl", {"PPFCF": _rows_to_frame([["PPFAS"]]), "HFCF": helios})
    with patch("mf_tracker.parsing.read_workbook", return_value=raw):
        with pytest.raises(ValidationError, match="ambiguous AMC"):
            parse_workbook(tmp_path / "ambiguous.xlsx")


def test_multiple_helios_fund_sheets_are_rejected_by_adapter(tmp_path):
    helios = _rows_to_frame([
        ["Helios Mutual Fund"],
        ["PORTFOLIO STATEMENT AS ON"],
        ["Name of the Instrument"],
    ])
    raw = RawWorkbook("openpyxl", {"HFCF": helios, "HMCF": helios})
    with patch("mf_tracker.parsing.read_workbook", return_value=raw):
        with pytest.raises(ValidationError, match="exactly one recognized Helios"):
            parse_workbook(tmp_path / "multiple.xlsx")
