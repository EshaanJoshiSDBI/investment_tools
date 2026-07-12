import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

import pytest

from mf_tracker.bundles import export_bundle, import_bundle
from mf_tracker.cli import main
from mf_tracker.domain import FundSnapshot, Holding, MetadataOverrides, ParsedWorkbook
from mf_tracker.errors import MigrationError, SnapshotConflictError, ValidationError
from mf_tracker.ingestion import ingest_directory, ingest_file
from mf_tracker.parsing import parse_workbook
from mf_tracker.persistence import SQLiteRepository, SourceArchive


def _holding(*, asset_class: str = "domestic_equity", name: str = "Original", quantity: float = 1) -> Holding:
    return Holding(
        "F", 1, None, name, name.casefold(), "INE040A01034", asset_class, "equity",
        None, None, None, quantity, 10, 0.1, identity_key="equity:isin:INE040A01034",
    )


def _parsed(path: Path, sha: str, month: int, *, parser: str = "v1", metadata=None,
            asset_class: str = "domestic_equity", name: str = "Original", quantity: float = 1) -> ParsedWorkbook:
    return ParsedWorkbook(
        path, sha, path.stat().st_size if path.exists() else 1, "openpyxl", date(2026, month, 1),
        [FundSnapshot("F", "Fund", date(2026, month, 1), [_holding(asset_class=asset_class, name=name, quantity=quantity)])],
        parser_version=parser, metadata_overrides=metadata or {},
    )


def test_ingestion_identity_includes_parser_and_metadata(tmp_path: Path):
    path = tmp_path / "source.xlsx"
    path.write_bytes(b"source")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    with SQLiteRepository(tmp_path / "db.sqlite") as repository:
        first = repository.save_workbook(_parsed(path, sha, 1))
        duplicate = repository.save_workbook(_parsed(path, sha, 1))
        changed_parser = repository.save_workbook(_parsed(path, sha, 2, parser="v2"))
        changed_metadata = repository.save_workbook(
            _parsed(path, sha, 3, metadata={"report_date": {"source": "x", "effective": "y"}})
        )
        assert [first.status, duplicate.status, changed_parser.status, changed_metadata.status] == [
            "ingested", "no_op", "ingested", "ingested",
        ]
        assert repository.connection.execute("SELECT count(*) FROM source_files").fetchone()[0] == 3


def test_historical_holding_attributes_are_immutable(tmp_path: Path):
    path = tmp_path / "source.xlsx"
    path.write_bytes(b"source")
    with SQLiteRepository(tmp_path / "db.sqlite") as repository:
        repository.save_workbook(_parsed(path, "a", 1, asset_class="domestic_equity", name="Old"))
        repository.save_workbook(_parsed(path, "b", 2, asset_class="cash_receivable", name="New"))
        january = repository.snapshot_frame(1, "2026-01-01").to_dicts()[0]
        assert january["asset_class"] == "domestic_equity"
        assert january["display_name"] == "Old"


def test_concurrent_duplicate_is_atomic_no_op(tmp_path: Path):
    path = tmp_path / "source.xlsx"
    path.write_bytes(b"source")
    database = tmp_path / "db.sqlite"
    with SQLiteRepository(database):
        pass

    def save() -> str:
        with SQLiteRepository(database) as repository:
            return repository.save_workbook(_parsed(path, "same", 1)).status

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(lambda _: save(), range(2)))
    assert statuses == ["ingested", "no_op"]


def test_archive_and_bundle_round_trip(tmp_path: Path):
    path = tmp_path / "source.xlsx"
    path.write_bytes(b"source bytes")
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    archive = SourceArchive(tmp_path / "sources")
    database = tmp_path / "source.sqlite"
    bundle = tmp_path / "export.mft.zip"
    with SQLiteRepository(database, source_archive=archive) as repository:
        repository.save_workbook(_parsed(path, sha, 1))
        replacement_path = tmp_path / "replacement.xlsx"
        replacement_path.write_bytes(b"replacement bytes")
        replacement_sha = hashlib.sha256(replacement_path.read_bytes()).hexdigest()
        repository.save_workbook(_parsed(replacement_path, replacement_sha, 1, quantity=2), replace=True)
        assert repository.archive_report() == {"missing": [], "corrupt": [], "unreferenced": []}
        export_bundle(repository, bundle)
    with SQLiteRepository(tmp_path / "import.sqlite", source_archive=SourceArchive(tmp_path / "import.sources")) as imported:
        import_bundle(bundle, imported)
        assert imported.connection.execute("SELECT count(*) FROM holdings").fetchone()[0] == 2
        states = imported.connection.execute(
            "SELECT lifecycle_status,superseded_by_snapshot_id FROM snapshots ORDER BY id"
        ).fetchall()
        assert states[0][0] == "superseded" and states[0][1] == 2
        assert states[1][0] == "active"
        assert imported.archive_report() == {"missing": [], "corrupt": [], "unreferenced": []}


def test_failed_conflict_does_not_leave_unreferenced_archive(tmp_path: Path):
    first = tmp_path / "first.xlsx"
    first.write_bytes(b"first")
    second = tmp_path / "second.xlsx"
    second.write_bytes(b"second")
    archive = SourceArchive(tmp_path / "sources")
    with SQLiteRepository(tmp_path / "db.sqlite", source_archive=archive) as repository:
        repository.save_workbook(_parsed(first, hashlib.sha256(first.read_bytes()).hexdigest(), 1))
        with pytest.raises(SnapshotConflictError, match="Snapshot already exists"):
            repository.save_workbook(_parsed(second, hashlib.sha256(second.read_bytes()).hexdigest(), 1))
        assert repository.archive_report()["unreferenced"] == []


def test_future_schema_is_rejected_without_downgrade(tmp_path: Path):
    database = tmp_path / "future.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE marker(id INTEGER)")
    connection.execute("PRAGMA user_version = 99")
    connection.commit()
    connection.close()
    with pytest.raises(MigrationError):
        SQLiteRepository(database)
    connection = sqlite3.connect(database)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 99
    connection.close()


def test_directory_validation_and_repositoryless_dry_run(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        ingest_directory(tmp_path / "missing", None, dry_run=True)
    file_path = tmp_path / "file"
    file_path.write_text("x")
    with pytest.raises(NotADirectoryError):
        ingest_directory(file_path, None, dry_run=True)
    assert ingest_directory(tmp_path, None, dry_run=True, pattern="*.xlsx").results == []


def test_cli_dry_run_does_not_create_database_or_archive(tmp_path: Path, monkeypatch):
    from mf_tracker import ingestion
    source = tmp_path / "source.xlsx"
    source.write_bytes(b"source")
    monkeypatch.setattr(ingestion, "parse_workbook", lambda *args, **kwargs: _parsed(source, "hash", 1))
    database = tmp_path / "dry.sqlite"
    assert main(["ingest-file", str(source), "--db", str(database), "--dry-run"]) == 0
    assert not database.exists()
    assert not Path(f"{database}.sources").exists()


def test_datetime_metadata_override_is_rejected_before_adapter_parse(tmp_path: Path, monkeypatch):
    from mf_tracker import parsing
    monkeypatch.setattr(parsing, "read_workbook", lambda _: object())
    with pytest.raises(ValidationError, match="datetime.date"):
        parse_workbook(tmp_path / "x.xlsx", metadata=MetadataOverrides(report_date=datetime(2026, 1, 1)))
