from abc import ABC, abstractmethod
import math

import pandas as pd

from portfolio.models import PriceResult


class PriceProvider(ABC):
    @abstractmethod
    def get_prices(self, symbols: list[str]) -> list[PriceResult]:
        """Return latest prices for symbols without failing the whole batch."""


class YFinancePriceProvider(PriceProvider):
    def get_prices(self, symbols: list[str]) -> list[PriceResult]:
        import yfinance as yf

        original_symbols = list(dict.fromkeys(symbol.strip().upper() for symbol in symbols))
        provider_symbols = {
            symbol: symbol if "." in symbol else f"{symbol}.NS"
            for symbol in original_symbols
        }
        try:
            history = yf.download(
                tickers=list(provider_symbols.values()),
                period="5d",
                interval="1d",
                auto_adjust=False,
                progress=False,
                threads=True,
                timeout=10,
            )
        except Exception as exc:  # yfinance can raise transport/provider errors.
            return [
                PriceResult(symbol=symbol, success=False, error=str(exc))
                for symbol in original_symbols
            ]

        results: list[PriceResult] = []
        for symbol, provider_symbol in provider_symbols.items():
            price = _latest_close(history, provider_symbol, len(provider_symbols) == 1)
            if price is None:
                results.append(
                    PriceResult(symbol=symbol, success=False, error="No valid price data found")
                )
            else:
                results.append(PriceResult(symbol=symbol, price=price, success=True))
        return results


def _latest_close(
    history: pd.DataFrame, provider_symbol: str, single_symbol: bool
) -> float | None:
    if history.empty:
        return None
    try:
        if isinstance(history.columns, pd.MultiIndex):
            close = history["Close"][provider_symbol]
        elif single_symbol:
            close = history["Close"]
        else:
            return None
        values = close.dropna()
        if values.empty:
            return None
        price = float(values.iloc[-1])
        return price if math.isfinite(price) and price > 0 else None
    except (KeyError, TypeError, ValueError, IndexError):
        return None
