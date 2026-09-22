from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

# ccxt digits-counting modes. Kept local so sizing math does not import ccxt
# until a public metadata fetch is actually requested.
DECIMAL_PLACES = 2
SIGNIFICANT_DIGITS = 3
TICK_SIZE = 4


@dataclass(frozen=True, slots=True)
class MarketConstraints:
    """Public venue limits used to validate a would-be order. No credentials."""

    min_qty: float = 0.0
    min_cost: float = 0.0
    amount_step: float = 0.0
    price_tick: float = 0.0
    last_price: float | None = None


class MarketMetadata(Protocol):
    def for_symbol(self, symbol: str) -> MarketConstraints: ...


class StaticMarketMetadata:
    """In-process constraints. Tests and the offline dry-run path use this."""

    def __init__(
        self,
        constraints: MarketConstraints | None = None,
        *,
        by_symbol: dict[str, MarketConstraints] | None = None,
    ) -> None:
        self._default = constraints or MarketConstraints()
        self._by_symbol = dict(by_symbol or {})

    def for_symbol(self, symbol: str) -> MarketConstraints:
        return self._by_symbol.get(symbol, self._default)


def step_from_precision(precision: object, precision_mode: int) -> float:
    """Convert a ccxt precision field into a step size. ``0`` means unconstrained."""
    if precision is None:
        return 0.0
    try:
        value = float(precision)
    except (TypeError, ValueError):
        return 0.0
    if value <= 0:
        return 0.0
    if precision_mode == TICK_SIZE:
        return value
    if precision_mode == DECIMAL_PLACES:
        return 10.0 ** (-value)
    if precision_mode == SIGNIFICANT_DIGITS:
        return 0.0
    if value < 1:
        return value
    if value == int(value) and value <= 18:
        return 10.0 ** (-value)
    return value


def _as_float(value: object, default: float = 0.0) -> float:
    if value is None or value == "":
        return default
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _assert_public(exchange: Any) -> None:
    for attr in ("apiKey", "secret", "password"):
        if getattr(exchange, attr, None):
            raise RuntimeError(
                "Dry-run refuses exchange credentials. "
                "Public market metadata only; no private endpoints and no API keys."
            )


class CcxtPublicMarketMetadata:
    """Min qty, tick, and last price from a public ccxt client.

    Only ``load_markets``, ``market``, and ``fetch_ticker`` are called.
    The client is constructed without keys. A non-empty credential is an error.
    """

    def __init__(self, exchange_id: str = "kraken", exchange: Any | None = None) -> None:
        self.exchange_id = exchange_id
        if exchange is None:
            import ccxt

            exchange_cls = getattr(ccxt, exchange_id, None)
            if exchange_cls is None:
                raise ValueError(f"unknown ccxt exchange id: {exchange_id}")
            exchange = exchange_cls({"enableRateLimit": True, "timeout": 20_000})
        _assert_public(exchange)
        self._exchange = exchange
        self._loaded = False
        self._cache: dict[str, MarketConstraints] = {}

    def for_symbol(self, symbol: str) -> MarketConstraints:
        cached = self._cache.get(symbol)
        if cached is not None:
            return cached
        if not self._loaded:
            try:
                self._exchange.load_markets()
            except Exception as exc:
                raise RuntimeError(
                    f"public market load failed for {self.exchange_id}: {exc}. "
                    "Dry-run does not use API keys or private endpoints."
                ) from exc
            self._loaded = True
        try:
            market = self._exchange.market(symbol)
        except Exception as exc:
            raise RuntimeError(f"public market metadata unavailable for {symbol}: {exc}") from exc
        limits = market.get("limits") or {}
        amount_limits = limits.get("amount") or {}
        cost_limits = limits.get("cost") or {}
        precision = market.get("precision") or {}
        mode = int(getattr(self._exchange, "precisionMode", TICK_SIZE))
        last_price: float | None = None
        try:
            ticker = self._exchange.fetch_ticker(symbol)
        except Exception:
            ticker = None
        if ticker:
            raw_last = ticker.get("last")
            if raw_last is None:
                raw_last = ticker.get("close")
            if raw_last not in (None, ""):
                try:
                    parsed = float(raw_last)
                except (TypeError, ValueError):
                    parsed = 0.0
                if parsed > 0:
                    last_price = parsed
        constraints = MarketConstraints(
            min_qty=_as_float(amount_limits.get("min")),
            min_cost=_as_float(cost_limits.get("min")),
            amount_step=step_from_precision(precision.get("amount"), mode),
            price_tick=step_from_precision(precision.get("price"), mode),
            last_price=last_price,
        )
        self._cache[symbol] = constraints
        return constraints
