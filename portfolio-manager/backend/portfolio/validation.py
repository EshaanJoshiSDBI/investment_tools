import math
from collections.abc import Iterable

import pandas as pd

from portfolio.models import MAX_PORTFOLIO_ITEMS, Holding, TargetWeight


MANDATORY_COLUMNS = ["Stock Symbol", "Qty", "Avg.Price", "LTP"]


class PortfolioValidationError(ValueError):
    """Raised when uploaded or requested portfolio data is invalid."""


def validate_required_columns(columns: Iterable[str]) -> None:
    column_set = {str(column).strip() for column in columns}
    missing = [column for column in MANDATORY_COLUMNS if column not in column_set]
    if missing:
        raise PortfolioValidationError(
            f"File must contain mandatory columns: {', '.join(missing)}"
        )


def validate_holdings_frame(df: pd.DataFrame) -> None:
    errors: list[str] = []

    if df.empty:
        raise PortfolioValidationError("Portfolio file must contain at least one holding")
    if len(df) > MAX_PORTFOLIO_ITEMS:
        raise PortfolioValidationError(
            f"Portfolio file cannot contain more than {MAX_PORTFOLIO_ITEMS:,} rows"
        )

    for index, row in df.iterrows():
        row_number = index + 2
        symbol = str(row.get("Stock Symbol", "")).strip()
        quantity = row.get("Qty")
        avg_price = row.get("Avg.Price")
        ltp = row.get("LTP")

        if not symbol or symbol.lower() == "nan":
            errors.append(f"Row {row_number}: Symbol cannot be empty")
        if not _is_finite(quantity) or quantity < 0:
            errors.append(f"Row {row_number}: Quantity must be >= 0")
        if not _is_finite(avg_price) or avg_price <= 0:
            errors.append(f"Row {row_number}: Avg price must be > 0")
        if not _is_finite(ltp) or ltp <= 0:
            errors.append(f"Row {row_number}: LTP must be > 0")

    if errors:
        raise PortfolioValidationError("; ".join(errors))


def validate_holdings(holdings: list[Holding], *, allow_empty: bool = False) -> None:
    if not holdings and not allow_empty:
        raise PortfolioValidationError("At least one holding is required")
    duplicates = _duplicates(holding.symbol for holding in holdings)
    if duplicates:
        raise PortfolioValidationError(
            f"Holdings contain duplicate symbols: {', '.join(duplicates)}"
        )


def validate_target_weights(
    target_weights: list[TargetWeight], holdings: list[Holding]
) -> dict[str, float]:
    duplicate_targets = _duplicates(item.symbol for item in target_weights)
    if duplicate_targets:
        raise PortfolioValidationError(
            f"Target weights contain duplicate symbols: {', '.join(duplicate_targets)}"
        )
    target_map = {item.symbol: item.target_weight_pct for item in target_weights}
    holding_symbols = {holding.symbol for holding in holdings}
    unknown_symbols = sorted(set(target_map) - holding_symbols)
    if unknown_symbols:
        raise PortfolioValidationError(
            f"Target weights include unknown symbols: {', '.join(unknown_symbols)}"
        )

    total_target_weight = sum(target_map.values())
    if total_target_weight > 100:
        raise PortfolioValidationError("Total target weight must be <= 100")

    return {symbol: target_map.get(symbol, 0.0) for symbol in holding_symbols}


def _is_finite(value: object) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _duplicates(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(duplicates)
