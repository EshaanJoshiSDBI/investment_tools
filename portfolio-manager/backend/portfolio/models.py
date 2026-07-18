from enum import Enum
import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


MAX_PORTFOLIO_ITEMS = 5_000
SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.&_-]*$")


class PortfolioModel(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)


def normalize_symbol(value: str) -> str:
    symbol = value.strip().upper()
    if not symbol:
        raise ValueError("Symbol cannot be empty")
    if not SYMBOL_PATTERN.fullmatch(symbol):
        raise ValueError("Symbol contains unsupported characters")
    return symbol


class RoundingMode(str, Enum):
    nearest = "nearest"
    floor = "floor"
    ceil = "ceil"


class Holding(PortfolioModel):
    symbol: str
    quantity: float = Field(ge=0)
    avg_price: float = Field(gt=0)
    ltp: float = Field(gt=0)
    value_at_cost: float = 0
    market_value: float = 0
    unrealized_pnl: float = 0
    unrealized_pnl_pct: float = 0
    current_weight_pct: float = 0

    @field_validator("symbol")
    @classmethod
    def symbol_required(cls, value: str) -> str:
        return normalize_symbol(value)


class PortfolioSummary(PortfolioModel):
    total_market_value: float
    total_cost: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    holding_count: int


class PortfolioUploadResponse(PortfolioModel):
    holdings: list[Holding]
    summary: PortfolioSummary


class SourceMetadata(PortfolioModel):
    filename: str
    file_size: int
    sha256: str
    parser_version: str
    imported_at: str


class SnapshotSummary(PortfolioModel):
    snapshot_id: str
    filename: str
    created_at: str
    lifecycle_status: Literal["active", "superseded"]
    restored_from_snapshot_id: Optional[str] = None


class PortfolioState(PortfolioUploadResponse):
    snapshot_id: str
    lifecycle_status: Literal["active", "superseded"]
    is_active: bool
    source: SourceMetadata
    target_weights: list["TargetWeight"]
    fresh_cash: float
    rounding_mode: RoundingMode
    latest_price_at: Optional[str] = None


class WorkspaceState(PortfolioModel):
    active: Optional[PortfolioState]
    snapshots: list[SnapshotSummary]


class UploadResult(PortfolioModel):
    status: Literal["ingested", "no_op"]
    workspace: WorkspaceState


class TargetWeight(PortfolioModel):
    symbol: str
    target_weight_pct: float = Field(ge=0, le=100)

    @field_validator("symbol")
    @classmethod
    def symbol_required(cls, value: str) -> str:
        return normalize_symbol(value)


class RebalanceRequest(PortfolioModel):
    holdings: list[Holding] = Field(min_length=1, max_length=MAX_PORTFOLIO_ITEMS)
    target_weights: list[TargetWeight] = Field(max_length=MAX_PORTFOLIO_ITEMS)
    fresh_cash: float = Field(default=0, ge=0)
    rounding_mode: RoundingMode = RoundingMode.nearest


class WorkingStateRequest(PortfolioModel):
    target_weights: list[TargetWeight] = Field(max_length=MAX_PORTFOLIO_ITEMS)
    fresh_cash: float = Field(default=0, ge=0)
    rounding_mode: RoundingMode = RoundingMode.nearest


class RebalanceRow(PortfolioModel):
    symbol: str
    quantity: float
    ltp: float
    current_weight_pct: float
    target_weight_pct: float
    target_qty: int
    trade_qty: float
    trade_value: float
    action: str
    final_market_value: float
    final_weight_pct: float
    weight_drift_pct: float


class CashImpact(PortfolioModel):
    fresh_cash: float
    total_buy_value: float
    total_sell_value: float
    net_cash_required: float
    cash_surplus_or_shortfall: float
    final_total_value: float


class RebalanceResponse(PortfolioModel):
    rows: list[RebalanceRow]
    cash_impact: CashImpact


class RefreshPricesRequest(PortfolioModel):
    symbols: list[str] = Field(min_length=1, max_length=MAX_PORTFOLIO_ITEMS)

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, values: list[str]) -> list[str]:
        return [normalize_symbol(value) for value in values]


class PriceResult(PortfolioModel):
    symbol: str
    price: Optional[float] = None
    success: bool
    error: Optional[str] = None


class RefreshPricesResponse(PortfolioModel):
    prices: list[PriceResult]


class PersistedRefreshResponse(RefreshPricesResponse):
    portfolio: PortfolioState
