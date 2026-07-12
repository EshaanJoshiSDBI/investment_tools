from datetime import date
from pathlib import Path

from mf_tracker.comparison import compare_snapshots
from mf_tracker.domain import FundSnapshot, Holding, ParsedWorkbook
from mf_tracker.persistence import SQLiteRepository


def position(quantity: float, *, market_value: float = 20.0) -> Holding:
    return Holding(
        "PPFCF", 7, "HDFB03", "HDFC Bank Limited", "hdfc bank limited",
        "INE040A01034", "domestic_equity", "equity", "Equity & Equity related",
        "Listed", "Banks", quantity, market_value, 0.1,
        identity_key="equity:isin:INE040A01034",
    )


def test_snapshot_comparison_is_computed_on_demand(tmp_path: Path):
    january = ParsedWorkbook(
        tmp_path / "jan.xls", "jan", 1, "openpyxl", date(2026, 1, 31),
        [FundSnapshot("PPFCF", "Fund", date(2026, 1, 31), [position(10)])],
    )
    february = ParsedWorkbook(
        tmp_path / "feb.xls", "feb", 1, "xlrd", date(2026, 2, 28),
        [FundSnapshot("PPFCF", "Fund", date(2026, 2, 28), [position(15, market_value=25)])],
    )
    with SQLiteRepository(tmp_path / "db.sqlite") as repository:
        repository.save_workbook(january)
        repository.save_workbook(february)
        fund_id = repository.connection.execute("SELECT id FROM funds").fetchone()[0]
        result = compare_snapshots(repository, fund_id, date(2026, 1, 31), date(2026, 2, 28))
        row = result.frame.row(0, named=True)
        assert row["change_type"] == "increased"
        assert row["quantity_delta"] == 5
        assert row["market_value_delta"] == 5
