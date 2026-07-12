import json
import sqlite3
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from mf_tracker.cli import main
from mf_tracker.domain import MetadataOverrides
from mf_tracker.errors import ValidationError
from mf_tracker.ingestion import ingest_directory, ingest_file
from mf_tracker.parsing import parse_workbook
from mf_tracker.persistence import SQLiteRepository

FIXTURES = Path(__file__).parents[1] / "sheets" / "oldbridge"
SOURCE = FIXTURES / "Old_Bridge_Focused_Fund_Monthly_Portfolio_Feb_26_4211b7c07a.xlsx"


@pytest.mark.skipif(not SOURCE.exists(), reason="Old Bridge fixture is not available")
def test_explicit_metadata_overrides_are_effective_and_audited(tmp_path: Path):
    metadata = MetadataOverrides(
        report_date=date(2026, 2, 27), fund_code="OBFCE",
        fund_name="Override Focused Fund", amc_name="Override AMC",
    )
    parsed = parse_workbook(SOURCE, amc="oldbridge", metadata=metadata)
    assert parsed.report_date == date(2026, 2, 27)
    assert parsed.snapshots[0].report_date == date(2026, 2, 27)
    assert parsed.snapshots[0].fund_name == "Override Focused Fund"
    assert parsed.amc_name == "Override AMC"
    assert set(parsed.metadata_overrides) == {"report_date", "fund_name", "amc_name"}
    assert sum(issue.code == "metadata_override" for issue in parsed.issues) == 3

    with SQLiteRepository(tmp_path / "metadata.sqlite3") as repository:
        result = ingest_file(SOURCE, repository, amc="oldbridge", metadata=metadata)
        assert result.effective_metadata["report_date"] == "2026-02-27"
        stored = repository.connection.execute(
            "SELECT metadata_overrides_json FROM source_files"
        ).fetchone()[0]
        assert json.loads(stored) == parsed.metadata_overrides


@pytest.mark.skipif(not SOURCE.exists(), reason="Old Bridge fixture is not available")
def test_invalid_amc_and_metadata_are_rejected():
    with pytest.raises(ValidationError, match="unsupported AMC"):
        parse_workbook(SOURCE, amc="unknown")
    with pytest.raises(ValidationError, match="unsupported AMC"):
        parse_workbook(SOURCE, amc="  ")
    with pytest.raises(ValidationError, match="must not be blank"):
        parse_workbook(SOURCE, metadata=MetadataOverrides(fund_name="  "))
    with pytest.raises(ValidationError, match="not valid for AMC oldbridge"):
        parse_workbook(SOURCE, amc="oldbridge", metadata=MetadataOverrides(fund_code="PPFCF"))
    with pytest.raises(ValidationError, match="does not match ppfas structure"):
        parse_workbook(SOURCE, amc="ppfas")


def test_directory_propagates_amc_to_each_file(tmp_path: Path):
    (tmp_path / "a.xlsx").write_bytes(b"x")
    result = Mock()
    with patch("mf_tracker.ingestion.ingest_file", return_value=result) as single:
        batch = ingest_directory(tmp_path, Mock(), dry_run=True, amc="oldbridge")
    assert batch.results == [result]
    assert single.call_args.kwargs["amc"] == "oldbridge"


def test_cli_rejects_metadata_flags_for_directory(tmp_path: Path):
    with pytest.raises(SystemExit):
        main(["ingest-directory", str(tmp_path), "--db", str(tmp_path / "x.sqlite3"),
              "--fund-name", "Not allowed"])


def test_schema_v1_is_migrated_additively(tmp_path: Path):
    database = tmp_path / "v1.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript("""
        PRAGMA user_version = 1;
        CREATE TABLE source_files (
            id INTEGER PRIMARY KEY, sha256 TEXT NOT NULL UNIQUE, filename TEXT NOT NULL,
            file_size INTEGER NOT NULL, report_date TEXT NOT NULL, parser_version TEXT NOT NULL,
            reader TEXT NOT NULL, status TEXT NOT NULL, ingested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO source_files(sha256,filename,file_size,report_date,parser_version,reader,status)
        VALUES('old','old.xls',1,'2026-01-31','v1','xlrd','ingested');
    """)
    connection.commit()
    connection.close()
    with SQLiteRepository(database) as repository:
        columns = {row[1] for row in repository.connection.execute("PRAGMA table_info(source_files)")}
        assert "metadata_overrides_json" in columns
        assert repository.connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert repository.connection.execute(
            "SELECT metadata_overrides_json FROM source_files WHERE sha256='old'"
        ).fetchone()[0] == "{}"
