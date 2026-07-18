from __future__ import annotations

import hashlib
import json
import math
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Protocol
from urllib.parse import quote, urlencode

from portfolio.errors import (
    KiteAuthenticationError,
    KiteCallbackError,
    KiteHoldingsError,
    KiteNotConfiguredError,
    KiteUpstreamError,
)
from portfolio.models import Holding, KiteStatus, normalize_symbol


KITE_PARSER_VERSION = "zerodha-kite-holdings-v1"
SUPPORTED_EXCHANGES = {"NSE", "BSE"}


@dataclass(frozen=True)
class KiteConfig:
    api_key: str = ""
    api_secret: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.api_secret)


@dataclass(frozen=True)
class KiteSession:
    access_token: str
    user_id: str
    login_time: str | None


@dataclass(frozen=True)
class NormalizedKitePortfolio:
    holdings: list[Holding]
    structural_fingerprint: str
    account_ref: str


class KiteGateway(Protocol):
    def login_url(self, api_key: str) -> str: ...

    def generate_session(
        self, api_key: str, request_token: str, api_secret: str
    ) -> dict[str, Any]: ...

    def holdings(
        self, api_key: str, access_token: str, on_expiry: Callable[[], None]
    ) -> list[dict[str, Any]]: ...


class OfficialKiteGateway:
    """Small boundary around the official SDK so tests never need live Kite access."""

    @staticmethod
    def _imports() -> tuple[type[Any], Any]:
        try:
            from kiteconnect import KiteConnect, exceptions
        except ImportError as exc:  # pragma: no cover - dependency installation failure.
            raise KiteUpstreamError("The Kite Connect dependency is not installed") from exc
        return KiteConnect, exceptions

    def login_url(self, api_key: str) -> str:
        KiteConnect, _ = self._imports()
        return str(KiteConnect(api_key=api_key).login_url())

    def generate_session(
        self, api_key: str, request_token: str, api_secret: str
    ) -> dict[str, Any]:
        KiteConnect, exceptions = self._imports()
        try:
            return dict(
                KiteConnect(api_key=api_key).generate_session(
                    request_token, api_secret=api_secret
                )
            )
        except Exception as exc:  # SDK exceptions do not share one safe public base.
            raise _translate_sdk_error(exc, exceptions) from exc

    def holdings(
        self, api_key: str, access_token: str, on_expiry: Callable[[], None]
    ) -> list[dict[str, Any]]:
        KiteConnect, exceptions = self._imports()
        client = KiteConnect(api_key=api_key, access_token=access_token)
        client.set_session_expiry_hook(on_expiry)
        try:
            return list(client.holdings())
        except Exception as exc:
            raise _translate_sdk_error(exc, exceptions) from exc


def _translate_sdk_error(exc: Exception, exceptions: Any) -> KiteUpstreamError:
    code = int(getattr(exc, "code", 0) or 0)
    if isinstance(exc, exceptions.TokenException) or code == 403:
        return KiteUpstreamError(
            "Kite session expired or was invalidated; reconnect to continue",
            status_code=401,
            token_expired=True,
        )
    if code == 429:
        return KiteUpstreamError("Kite rate limit exceeded; try again shortly", status_code=429)
    if isinstance(exc, exceptions.InputException) or code == 400:
        return KiteUpstreamError("Kite rejected the request", status_code=400)
    return KiteUpstreamError("Kite could not return portfolio holdings", status_code=502)


class KiteService:
    def __init__(
        self,
        config: KiteConfig,
        gateway: KiteGateway | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        nonce_ttl_seconds: int = 600,
    ):
        self.config = config
        self.gateway = gateway or OfficialKiteGateway()
        self.clock = clock
        self.nonce_ttl_seconds = nonce_ttl_seconds
        self._lock = threading.RLock()
        self._pending_states: dict[str, float] = {}
        self._session: KiteSession | None = None

    def status(self) -> KiteStatus:
        with self._lock:
            session = self._session
            return KiteStatus(
                configured=self.config.configured,
                authenticated=session is not None,
                login_time=session.login_time if session else None,
            )

    def begin_login(self) -> str:
        self._require_configured()
        state = secrets.token_urlsafe(32)
        now = self.clock()
        with self._lock:
            self._pending_states = {
                key: expiry for key, expiry in self._pending_states.items() if expiry > now
            }
            self._pending_states[state] = now + self.nonce_ttl_seconds
        base = self.gateway.login_url(self.config.api_key)
        separator = "&" if "?" in base else "?"
        redirect_params = urlencode({"state": state})
        return f"{base}{separator}redirect_params={quote(redirect_params, safe='')}"

    def complete_login(self, *, state: str, request_token: str, status: str) -> None:
        self._require_configured()
        if status.lower() != "success":
            raise KiteCallbackError("Kite login was not completed successfully")
        now = self.clock()
        with self._lock:
            expiry = self._pending_states.pop(state, None)
        if expiry is None or expiry <= now:
            raise KiteCallbackError("Kite login state is invalid, expired, or already used")
        if not request_token:
            raise KiteCallbackError("Kite callback did not include a request token")

        data = self.gateway.generate_session(
            self.config.api_key, request_token, self.config.api_secret
        )
        access_token = str(data.get("access_token") or "")
        user_id = str(data.get("user_id") or "")
        if not access_token or not user_id:
            raise KiteAuthenticationError("Kite did not return a valid authenticated session")
        login_time = _login_time(data.get("login_time"))
        with self._lock:
            self._session = KiteSession(access_token, user_id, login_time)

    def disconnect(self) -> None:
        with self._lock:
            self._session = None
            self._pending_states.clear()

    def fetch_holdings(self) -> NormalizedKitePortfolio:
        self._require_configured()
        with self._lock:
            session = self._session
        if session is None:
            raise KiteAuthenticationError("Connect to Kite before importing holdings")
        try:
            rows = self.gateway.holdings(
                self.config.api_key, session.access_token, self.disconnect
            )
        except KiteUpstreamError as exc:
            if exc.token_expired:
                self.disconnect()
            raise
        account_ref = hashlib.sha256(
            f"{self.config.api_key}:{session.user_id}".encode()
        ).hexdigest()
        return normalize_kite_holdings(rows, account_ref)

    def _require_configured(self) -> None:
        if not self.config.configured:
            raise KiteNotConfiguredError(
                "Set KITE_API_KEY and KITE_API_SECRET in portfolio-manager/.env"
            )


def normalize_kite_holdings(
    rows: list[dict[str, Any]], account_ref: str
) -> NormalizedKitePortfolio:
    holdings: list[Holding] = []
    errors: list[str] = []
    seen: set[str] = set()

    for index, row in enumerate(rows, start=1):
        label = str(row.get("tradingsymbol") or f"row {index}").strip().upper()
        try:
            quantities = {
                "quantity": _number(row.get("quantity", 0), "quantity", minimum=0),
                "t1_quantity": _number(row.get("t1_quantity", 0), "t1_quantity", minimum=0),
                "collateral_quantity": _number(
                    row.get("collateral_quantity", 0), "collateral_quantity", minimum=0
                ),
            }
            mtf = row.get("mtf") or {}
            if not isinstance(mtf, dict):
                raise ValueError("mtf must be an object")
            mtf_quantity = _number(mtf.get("quantity", 0), "mtf.quantity", minimum=0)
            regular_quantity = sum(quantities.values())
            total_quantity = regular_quantity + mtf_quantity
            if total_quantity == 0:
                continue
            if row.get("discrepancy") is True:
                raise ValueError("has a price discrepancy")

            exchange = str(row.get("exchange") or "").strip().upper()
            if exchange not in SUPPORTED_EXCHANGES:
                raise ValueError(f"uses unsupported exchange {exchange or '(empty)'}")
            trading_symbol = normalize_symbol(str(row.get("tradingsymbol") or ""))
            symbol = trading_symbol if exchange == "NSE" else f"{exchange}:{trading_symbol}"
            symbol = normalize_symbol(symbol)
            if symbol in seen:
                raise ValueError(f"duplicates canonical holding {symbol}")

            regular_price = 0.0
            if regular_quantity:
                regular_price = _number(row.get("average_price"), "average_price", positive=True)
            mtf_price = 0.0
            if mtf_quantity:
                mtf_price = _number(mtf.get("average_price"), "mtf.average_price", positive=True)
            avg_price = (
                regular_quantity * regular_price + mtf_quantity * mtf_price
            ) / total_quantity

            ltp = _positive_or_none(row.get("last_price"))
            if ltp is None:
                ltp = _positive_or_none(row.get("close_price"))
            if ltp is None:
                raise ValueError("has no positive last or close price")

            holdings.append(
                Holding(
                    symbol=symbol,
                    quantity=total_quantity,
                    avg_price=avg_price,
                    ltp=ltp,
                )
            )
            seen.add(symbol)
        except (TypeError, ValueError) as exc:
            errors.append(f"{label}: {exc}")

    if errors:
        raise KiteHoldingsError("Invalid Kite holdings: " + "; ".join(errors))

    structural_rows = [
        {
            "symbol": holding.symbol,
            "quantity": _canonical_number(holding.quantity),
            "avg_price": _canonical_number(holding.avg_price),
        }
        for holding in sorted(holdings, key=lambda item: item.symbol)
    ]
    structural_payload = json.dumps(
        {"account_ref": account_ref, "holdings": structural_rows},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return NormalizedKitePortfolio(
        holdings=holdings,
        structural_fingerprint=hashlib.sha256(structural_payload).hexdigest(),
        account_ref=account_ref,
    )


def _number(
    value: Any,
    field: str,
    *,
    minimum: float | None = None,
    positive: bool = False,
) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    if positive and number <= 0:
        raise ValueError(f"{field} must be > 0")
    if minimum is not None and number < minimum:
        raise ValueError(f"{field} must be >= {minimum:g}")
    return number


def _positive_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _canonical_number(value: float) -> str:
    return format(value, ".12g")


def _login_time(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
