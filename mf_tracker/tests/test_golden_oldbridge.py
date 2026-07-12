from pathlib import Path

import pytest

from mf_tracker.amcs.oldbridge import FUND_NAMES
from mf_tracker.ingestion import ingest_directory
from mf_tracker.parsing import parse_workbook
from mf_tracker.persistence import SQLiteRepository

FIXTURES = Path(__file__).parents[1] / "sheets" / "oldbridge"


@pytest.mark.skipif(not FIXTURES.exists(), reason="Old Bridge source workbooks are not available")
def test_all_supplied_oldbridge_workbooks(tmp_path: Path):
    paths = sorted(FIXTURES.glob("*.xlsx"))
    parsed = [parse_workbook(path) for path in paths]
    assert len(parsed) == 10
    assert {workbook.reader for workbook in parsed} == {"openpyxl"}
    assert {workbook.amc_slug for workbook in parsed} == {"oldbridge"}
    assert {workbook.parser_version for workbook in parsed} == {"oldbridge-v1"}
    assert {workbook.report_date.month for workbook in parsed} == set(range(1, 7))
    snapshots = [workbook.snapshots[0] for workbook in parsed]
    assert {snapshot.sheet_code for snapshot in snapshots} == set(FUND_NAMES)
    assert {snapshot.fund_name for snapshot in snapshots} == set(FUND_NAMES.values())
    assert sum(len(snapshot.holdings) for snapshot in snapshots) == 291
    assert all(snapshot.reported_total_weight == pytest.approx(1.0) for snapshot in snapshots)
    assert all(sum(h.weight for h in snapshot.holdings) == pytest.approx(1.0, abs=0.02) for snapshot in snapshots)
    assert {h.asset_class for snapshot in snapshots for h in snapshot.holdings} == {
        "domestic_equity", "repo_treps", "cash_receivable",
    }
    assert any(h.market_value_lakh < 0 for snapshot in snapshots for h in snapshot.holdings)

    march_focused = next(
        s for s in snapshots if s.sheet_code == "OBFCE" and s.report_date.month == 3
    )
    assert all(h.source_instrument_code is None for h in march_focused.holdings)
    january = next(s for s in snapshots if s.sheet_code == "OBFCE" and s.report_date.month == 1)
    assert january.holdings[1].identity_key == "equity:isin:INE860A01027"

    with SQLiteRepository(tmp_path / "golden.sqlite3") as repository:
        first = ingest_directory(FIXTURES, repository, amc="oldbridge")
        second = ingest_directory(FIXTURES, repository, amc="oldbridge")
        assert all(result.status == "ingested" for result in first.results)
        assert all(result.status == "no_op" for result in second.results)
        assert repository.connection.execute("SELECT count(*) FROM source_files").fetchone()[0] == 10
        assert repository.connection.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 10
        assert repository.connection.execute("SELECT count(*) FROM funds").fetchone()[0] == 2
        assert repository.connection.execute("SELECT count(*) FROM holdings").fetchone()[0] == 291

