from pathlib import Path

import pytest

from mf_tracker.amcs.helios import FUND_NAMES, parse_workbook
from mf_tracker.ingestion import ingest_directory
from mf_tracker.persistence import SQLiteRepository

FIXTURES = Path(__file__).parents[1] / "sheets" / "helios"


@pytest.mark.skipif(not FIXTURES.exists(), reason="Helios source workbooks are not available")
def test_all_supplied_helios_workbooks(tmp_path: Path):
    paths = sorted(FIXTURES.glob("*.xlsx"))
    parsed = [parse_workbook(path) for path in paths]
    assert len(parsed) == 24
    assert {workbook.reader for workbook in parsed} == {"openpyxl"}
    assert {workbook.amc_slug for workbook in parsed} == {"helios"}
    assert {workbook.parser_version for workbook in parsed} == {"helios-v1"}
    assert {workbook.report_date.month for workbook in parsed} == set(range(1, 7))
    snapshots = [workbook.snapshots[0] for workbook in parsed]
    assert {snapshot.sheet_code for snapshot in snapshots} == set(FUND_NAMES)
    assert {snapshot.fund_name for snapshot in snapshots} == set(FUND_NAMES.values())
    assert sum(len(snapshot.holdings) for snapshot in snapshots) == 1455
    assert all(snapshot.reported_total_weight == pytest.approx(1.0) for snapshot in snapshots)
    assert all(sum(holding.weight for holding in snapshot.holdings) == pytest.approx(1.0, abs=0.02) for snapshot in snapshots)
    assert any(holding.market_value_lakh < 0 for snapshot in snapshots for holding in snapshot.holdings)

    with SQLiteRepository(tmp_path / "golden.sqlite3") as repository:
        first = ingest_directory(FIXTURES, repository)
        second = ingest_directory(FIXTURES, repository)
        assert all(result.status == "ingested" and result.amc_slug == "helios" for result in first.results)
        assert all(result.status == "no_op" for result in second.results)
        assert repository.connection.execute("SELECT count(*) FROM source_files").fetchone()[0] == 24
        assert repository.connection.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 24
        assert repository.connection.execute("SELECT count(*) FROM funds").fetchone()[0] == 4
        assert repository.connection.execute("SELECT count(*) FROM holdings").fetchone()[0] == 1455
        assert repository.connection.execute("SELECT parser_version FROM source_files LIMIT 1").fetchone()[0] == "helios-v1"
