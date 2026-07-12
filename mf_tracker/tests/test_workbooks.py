from pathlib import Path

import pytest

from mf_tracker.errors import UnsupportedWorkbookError
from mf_tracker.workbooks import detect_workbook_format


def test_detects_signatures_not_extensions(tmp_path: Path):
    fake_xls = tmp_path / "wrong.xls"
    fake_xls.write_bytes(b"PK\x03\x04more")
    assert detect_workbook_format(fake_xls) == "ooxml"
    legacy = tmp_path / "legacy.xlsx"
    legacy.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + b"more")
    assert detect_workbook_format(legacy) == "xls"


def test_rejects_unknown_signature(tmp_path: Path):
    path = tmp_path / "bad.xls"
    path.write_bytes(b"not excel")
    with pytest.raises(UnsupportedWorkbookError):
        detect_workbook_format(path)

