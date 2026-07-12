from datetime import date
from pathlib import Path

import pytest

from mf_tracker.domain import FundSnapshot, Holding, ParsedWorkbook
from mf_tracker.errors import SnapshotConflictError
from mf_tracker.persistence import SQLiteRepository


def holding() -> Holding:
    return Holding("PPFCF", 7, "HDFB03", "HDFC Bank Limited", "hdfc bank limited", "INE040A01034",
                   "domestic_equity", "equity", "Equity & Equity related", "Listed", "Banks",
                   10.0, 20.0, 0.1, identity_key="equity:isin:INE040A01034")


def test_save_and_hash_idempotency(tmp_path: Path):
    parsed = ParsedWorkbook(tmp_path / "a.xls", "abc", 1, "openpyxl", date(2026, 1, 31),
                            [FundSnapshot("PPFCF", "Fund", date(2026, 1, 31), [holding()])])
    with SQLiteRepository(tmp_path / "db.sqlite") as repository:
        repository.save_workbook(parsed)
        assert repository.has_source("abc")
        assert repository.connection.execute("SELECT count(*) FROM holdings").fetchone()[0] == 1


def test_conflict_and_atomic_replace(tmp_path: Path):
    first = ParsedWorkbook(tmp_path / "a.xls", "first", 1, "openpyxl", date(2026, 1, 31),
                           [FundSnapshot("PPFCF", "Fund", date(2026, 1, 31), [holding()])])
    replacement_holding = holding()
    replacement_holding.quantity = 99
    second = ParsedWorkbook(tmp_path / "b.xls", "second", 1, "openpyxl", date(2026, 1, 31),
                            [FundSnapshot("PPFCF", "Fund", date(2026, 1, 31), [replacement_holding])])
    with SQLiteRepository(tmp_path / "db.sqlite") as repository:
        repository.save_workbook(first)
        with pytest.raises(SnapshotConflictError):
            repository.save_workbook(second)
        assert repository.connection.execute("SELECT count(*) FROM source_files").fetchone()[0] == 1
        repository.save_workbook(second, replace=True)
        rows = repository.connection.execute(
            "SELECT s.lifecycle_status,h.quantity FROM snapshots s JOIN holdings h ON h.snapshot_id=s.id ORDER BY s.id"
        ).fetchall()
        assert [tuple(row) for row in rows] == [("superseded", 10), ("active", 99)]


def test_workbooks_are_persisted_under_their_parsed_amc(tmp_path: Path):
    ppfas = ParsedWorkbook(
        tmp_path / "ppfas.xls", "ppfas", 1, "xlrd", date(2026, 1, 31),
        [FundSnapshot("PPFCF", "PPFAS Flexi Cap Fund", date(2026, 1, 31), [holding()])],
    )
    helios_holding = holding()
    helios_holding.sheet_code = "HFCF"
    helios = ParsedWorkbook(
        tmp_path / "helios.xlsx", "helios", 1, "openpyxl", date(2026, 1, 31),
        [FundSnapshot("HFCF", "Helios Flexi Cap Fund", date(2026, 1, 31), [helios_holding])],
        amc_slug="helios", amc_name="Helios Mutual Fund", parser_version="helios-v1",
    )
    with SQLiteRepository(tmp_path / "db.sqlite") as repository:
        repository.save_workbook(ppfas)
        repository.save_workbook(helios)
        slugs = repository.connection.execute("SELECT slug FROM amcs ORDER BY slug").fetchall()
        assert [row[0] for row in slugs] == ["helios", "ppfas"]
        versions = repository.connection.execute("SELECT parser_version FROM source_files ORDER BY parser_version").fetchall()
        assert [row[0] for row in versions] == ["helios-v1", "ppfas-v1"]
