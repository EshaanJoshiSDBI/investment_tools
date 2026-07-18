from io import BytesIO
import math
from pathlib import Path

import pandas as pd

from portfolio.calculations import calculate_holdings, calculate_summary
from portfolio.models import Holding, PortfolioUploadResponse
from portfolio.validation import (
    PortfolioValidationError,
    validate_holdings_frame,
    validate_required_columns,
)


REQUIRED_RENAME_MAP = {
    "Stock Symbol": "symbol",
    "Qty": "quantity",
    "Avg.Price": "avg_price",
    "LTP": "ltp",
}

ZERODHA_HOLDINGS_RENAME_MAP = {
    "Instrument": "Stock Symbol",
    "Qty.": "Qty",
    "Avg. cost": "Avg.Price",
}
ZERODHA_HOLDINGS_COLUMNS = {*ZERODHA_HOLDINGS_RENAME_MAP, "LTP"}
ZERODHA_RENAMED_COLUMNS = set(ZERODHA_HOLDINGS_RENAME_MAP.values())


def read_portfolio_file(filename: str, content: bytes) -> pd.DataFrame:
    suffix = Path(filename).suffix.lower()
    buffer = BytesIO(content)

    if suffix == ".csv":
        return pd.read_csv(buffer, encoding="utf-8-sig")
    if suffix == ".xlsx":
        return pd.read_excel(buffer, engine="openpyxl")
    if suffix == ".xls":
        if _looks_like_text_table(content):
            return pd.read_csv(buffer, encoding="utf-8-sig")
        return pd.read_excel(buffer, engine="xlrd")

    raise PortfolioValidationError("Only CSV, XLSX, and XLS files are supported")


def _looks_like_text_table(content: bytes) -> bool:
    sample = content[:2048].lstrip(b"\xef\xbb\xbf\xff\xfe\xfe\xff")
    if not sample:
        return False
    if b"\x00" in sample:
        return False
    return any(delimiter in sample for delimiter in (b",", b"\t", b";"))


def normalize_portfolio_frame(df: pd.DataFrame) -> list[Holding]:
    df = df.copy()
    df.columns = [str(column).strip() for column in df.columns]
    df = normalize_source_columns(df)
    validate_required_columns(df.columns)

    normalized = df[list(REQUIRED_RENAME_MAP)].copy()
    for column in ["Qty", "Avg.Price", "LTP"]:
        normalized[column] = pd.to_numeric(normalized[column], errors="coerce")

    validate_holdings_frame(normalized)
    normalized = normalized.rename(columns=REQUIRED_RENAME_MAP)
    normalized["symbol"] = normalized["symbol"].astype(str).str.strip().str.upper()
    normalized = _aggregate_duplicate_symbols(normalized)

    holdings = [Holding(**record) for record in normalized.to_dict(orient="records")]
    return calculate_holdings(holdings)


def normalize_source_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Translate known broker export schemas to the canonical upload schema."""
    columns = set(df.columns)
    if (
        ZERODHA_HOLDINGS_COLUMNS.issubset(columns)
        and ZERODHA_RENAMED_COLUMNS.isdisjoint(columns)
    ):
        return df.rename(columns=ZERODHA_HOLDINGS_RENAME_MAP)
    return df


def _aggregate_duplicate_symbols(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, float | str]] = []
    for symbol, group in df.groupby("symbol", sort=False):
        prices = group["ltp"].tolist()
        first_price = float(prices[0])
        if any(not math.isclose(float(price), first_price) for price in prices[1:]):
            raise PortfolioValidationError(
                f"Duplicate symbol {symbol} has conflicting LTP values"
            )

        total_quantity = float(group["quantity"].sum())
        total_cost = float((group["quantity"] * group["avg_price"]).sum())
        avg_price = (
            total_cost / total_quantity
            if total_quantity > 0
            else float(group["avg_price"].iloc[0])
        )
        rows.append(
            {
                "symbol": symbol,
                "quantity": total_quantity,
                "avg_price": avg_price,
                "ltp": first_price,
            }
        )
    return pd.DataFrame(rows)


def load_portfolio(filename: str, content: bytes) -> PortfolioUploadResponse:
    df = read_portfolio_file(filename, content)
    holdings = normalize_portfolio_frame(df)
    return PortfolioUploadResponse(
        holdings=holdings,
        summary=calculate_summary(holdings),
    )
