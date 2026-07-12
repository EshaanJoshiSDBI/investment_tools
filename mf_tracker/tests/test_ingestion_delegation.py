from pathlib import Path
from unittest.mock import Mock, patch

from mf_tracker.domain import IngestionResult
from mf_tracker.ingestion import ingest_directory


def test_directory_delegates_to_single_file(tmp_path: Path):
    (tmp_path / "b.xls").write_bytes(b"x")
    (tmp_path / "a.xlsx").write_bytes(b"x")
    result = IngestionResult("x", "h", "2026-01-01", "openpyxl", "validated", 1, 1, {})
    with patch("mf_tracker.ingestion.ingest_file", return_value=result) as single:
        batch = ingest_directory(tmp_path, Mock(), dry_run=True)
    assert [call.args[0].name for call in single.call_args_list] == ["a.xlsx", "b.xls"]
    assert len(batch.results) == 2

