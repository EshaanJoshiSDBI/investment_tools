import math

from portfolio.models import (
    CashImpact,
    Holding,
    PortfolioSummary,
    RebalanceResponse,
    RebalanceRow,
    RoundingMode,
)


def round_quantity(value: float, mode: RoundingMode) -> int:
    if mode == RoundingMode.floor:
        return math.floor(value)
    if mode == RoundingMode.ceil:
        return math.ceil(value)
    return round(value)


def calculate_holdings(holdings: list[Holding]) -> list[Holding]:
    total_market_value = sum(holding.quantity * holding.ltp for holding in holdings)
    calculated: list[Holding] = []
    for holding in holdings:
        value_at_cost = holding.quantity * holding.avg_price
        market_value = holding.quantity * holding.ltp
        unrealized_pnl = market_value - value_at_cost
        calculated.append(
            holding.model_copy(
                update={
                    "value_at_cost": value_at_cost,
                    "market_value": market_value,
                    "unrealized_pnl": unrealized_pnl,
                    "unrealized_pnl_pct": (
                        unrealized_pnl / value_at_cost * 100 if value_at_cost else 0
                    ),
                    "current_weight_pct": (
                        market_value / total_market_value * 100
                        if total_market_value > 0
                        else 0
                    ),
                }
            )
        )
    return calculated


def calculate_summary(holdings: list[Holding]) -> PortfolioSummary:
    total_market_value = sum(holding.market_value for holding in holdings)
    total_cost = sum(holding.value_at_cost for holding in holdings)
    unrealized_pnl = total_market_value - total_cost
    unrealized_pnl_pct = (unrealized_pnl / total_cost * 100) if total_cost else 0

    return PortfolioSummary(
        total_market_value=total_market_value,
        total_cost=total_cost,
        unrealized_pnl=unrealized_pnl,
        unrealized_pnl_pct=unrealized_pnl_pct,
        holding_count=len(holdings),
    )


def calculate_rebalance(
    holdings: list[Holding],
    target_weights: dict[str, float],
    fresh_cash: float,
    rounding_mode: RoundingMode,
) -> RebalanceResponse:
    calculated = calculate_holdings(holdings)
    total_market_value = sum(holding.market_value for holding in calculated)
    target_portfolio_value = total_market_value + fresh_cash

    rows: list[RebalanceRow] = []
    for holding in calculated:
        target_weight_pct = target_weights.get(holding.symbol, 0)
        target_value = target_weight_pct / 100 * target_portfolio_value
        target_qty_float = target_value / holding.ltp
        target_qty = round_quantity(target_qty_float, rounding_mode)
        trade_qty = target_qty - holding.quantity
        trade_value = trade_qty * holding.ltp
        action = "HOLD"
        if trade_qty > 0:
            action = "BUY"
        elif trade_qty < 0:
            action = "SELL"

        final_market_value = target_qty * holding.ltp
        rows.append(
            RebalanceRow(
                symbol=holding.symbol,
                quantity=holding.quantity,
                ltp=holding.ltp,
                current_weight_pct=holding.current_weight_pct,
                target_weight_pct=target_weight_pct,
                target_qty=target_qty,
                trade_qty=trade_qty,
                trade_value=trade_value,
                action=action,
                final_market_value=final_market_value,
                final_weight_pct=0,
                weight_drift_pct=0,
            )
        )

    final_total_value = sum(row.final_market_value for row in rows)
    final_total_for_weights = target_portfolio_value if target_portfolio_value > 0 else 0
    normalized_rows: list[RebalanceRow] = []
    for row in rows:
        final_weight_pct = (
            row.final_market_value / final_total_for_weights * 100
            if final_total_for_weights
            else 0
        )
        normalized_rows.append(
            row.model_copy(
                update={
                    "final_weight_pct": final_weight_pct,
                    "weight_drift_pct": final_weight_pct - row.target_weight_pct,
                }
            )
        )

    total_buy_value = sum(row.trade_value for row in normalized_rows if row.trade_value > 0)
    total_sell_value = abs(
        sum(row.trade_value for row in normalized_rows if row.trade_value < 0)
    )
    net_cash_required = total_buy_value - total_sell_value

    return RebalanceResponse(
        rows=normalized_rows,
        cash_impact=CashImpact(
            fresh_cash=fresh_cash,
            total_buy_value=total_buy_value,
            total_sell_value=total_sell_value,
            net_cash_required=net_cash_required,
            cash_surplus_or_shortfall=fresh_cash - net_cash_required,
            final_total_value=final_total_value,
        ),
    )
