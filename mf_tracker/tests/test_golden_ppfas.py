from pathlib import Path

import pytest

from mf_tracker.amcs.ppfas import parse_workbook
from mf_tracker.ingestion import ingest_directory
from mf_tracker.persistence import SQLiteRepository

FIXTURES = Path("/Users/eshaan/dev_personal/investment_tools/mf_tracker/sheets/ppfas")


@pytest.mark.skipif(not FIXTURES.exists(), reason="PPFAS source workbooks are not available")
def test_all_supplied_ppfas_workbooks(tmp_path: Path):
    paths = sorted(FIXTURES.glob("*.xls"))
    parsed = [parse_workbook(path) for path in paths]
    assert len(parsed) == 6
    assert {workbook.reader for workbook in parsed} == {"openpyxl", "xlrd"}
    assert sum(len(workbook.snapshots) for workbook in parsed) == 41
    january = next(workbook for workbook in parsed if workbook.report_date.month == 1)
    assert {snapshot.sheet_code for snapshot in january.snapshots} == {
        "PPFCF", "PPLF", "PPTSF", "PPCHF", "PPAF", "PPDAAF"
    }
    classes = {
        holding.asset_class
        for workbook in parsed
        for snapshot in workbook.snapshots
        for holding in snapshot.holdings
    }
    assert {
        "domestic_equity", "foreign_equity", "corporate_debt",
        "government_security", "money_market", "mutual_fund_unit",
        "repo_treps", "cash_receivable", "equity_future", "index_future",
    } <= classes

    with SQLiteRepository(tmp_path / "golden.sqlite3") as repository:
        first = ingest_directory(FIXTURES, repository)
        second = ingest_directory(FIXTURES, repository)
        assert all(result.status == "ingested" for result in first.results)
        assert all(result.status == "no_op" for result in second.results)
        assert repository.connection.execute("SELECT count(*) FROM source_files").fetchone()[0] == 6
        assert repository.connection.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 41
        assert repository.connection.execute("SELECT count(*) FROM holdings").fetchone()[0] == 4650
