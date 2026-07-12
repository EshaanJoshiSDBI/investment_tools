from __future__ import annotations

from datetime import date

import polars as pl

from .domain import ComparisonResult
from .persistence import SQLiteRepository

DEFAULT_PRODUCT_ASSET_CLASSES = ["domestic_equity", "foreign_equity", "equity_future", "index_future"]


def _metric(asset_class: str) -> str:
    if asset_class in {"cash_receivable", "repo_treps"}:
        return "market_value_lakh"
    return "quantity"


def compare_snapshots(repository: SQLiteRepository, fund_id: int, from_date: date, to_date: date,
                      asset_classes: list[str] | None = None) -> ComparisonResult:
    before = repository.snapshot_frame(fund_id, from_date.isoformat())
    after = repository.snapshot_frame(fund_id, to_date.isoformat())
    selected = asset_classes or DEFAULT_PRODUCT_ASSET_CLASSES
    if before.is_empty() and after.is_empty():
        return ComparisonResult(fund_id, from_date, to_date, pl.DataFrame())
    if not before.is_empty():
        before = before.filter(pl.col("asset_class").is_in(selected))
    if not after.is_empty():
        after = after.filter(pl.col("asset_class").is_in(selected))
    keys = ["identity_key"]
    joined = before.join(after, on=keys, how="full", suffix="_to", coalesce=True)
    for column in ("quantity", "market_value_lakh", "weight", "ytm", "ytc"):
        if column not in joined.columns:
            joined = joined.with_columns(pl.lit(None).alias(column))
        if f"{column}_to" not in joined.columns:
            joined = joined.with_columns(pl.lit(None).alias(f"{column}_to"))
    joined = joined.with_columns(
        pl.coalesce([pl.col("asset_class_to"), pl.col("asset_class")]).alias("asset_class_effective"),
        (pl.col("quantity_to") - pl.col("quantity")).alias("quantity_delta"),
        (pl.col("market_value_lakh_to") - pl.col("market_value_lakh")).alias("market_value_delta"),
        (pl.col("weight_to") - pl.col("weight")).alias("weight_delta"),
        (pl.col("ytm_to") - pl.col("ytm")).alias("ytm_delta"),
        (pl.col("ytc_to") - pl.col("ytc")).alias("ytc_delta"),
    )
    metric_before = pl.when(pl.col("asset_class_effective").is_in(["cash_receivable", "repo_treps"])).then(pl.col("market_value_lakh")).otherwise(pl.col("quantity"))
    metric_after = pl.when(pl.col("asset_class_effective").is_in(["cash_receivable", "repo_treps"])).then(pl.col("market_value_lakh_to")).otherwise(pl.col("quantity_to"))
    joined = joined.with_columns(
        pl.when(metric_before.is_null()).then(pl.lit("introduced"))
        .when(metric_after.is_null()).then(pl.lit("exited"))
        .when(metric_after > metric_before).then(pl.lit("increased"))
        .when(metric_after < metric_before).then(pl.lit("decreased"))
        .otherwise(pl.lit("unchanged")).alias("change_type")
    )
    return ComparisonResult(fund_id, from_date, to_date, joined.sort(["asset_class_effective", "identity_key"]))

