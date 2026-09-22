"""Kraken live execution for ``sma_cross_v2``.

Orders are sent only when a caller passes ``enable_trading=True`` together with
an exchange client. ``quanttrading live`` is that caller. ``quanttrading paper``
and ``quanttrading dry-run`` never construct a trading client.

The unconfigured ``LiveBroker()`` still raises and does not load keys.
"""

from __future__ import annotations

import hashlib
from typing import Any

from quanttrading.config import Settings
from quanttrading.execution.base import ExecutionReport
from quanttrading.execution.dry_run import build_would_be_order
from quanttrading.execution.fills import Fill, estimated_notional, market_anchor_price, to_base
from quanttrading.execution.market_meta import MarketConstraints, MarketMetadata, StaticMarketMetadata
from quanttrading.execution.paper import PaperBroker, Position
from quanttrading.market import Bar
from quanttrading.signals import Signal
from quanttrading.strategy.base import Strategy, StrategyContext
from quanttrading.strategy.sma import DEFAULT_STRATEGY_ID as LIVE_STRATEGY_ID

LIVE_PER_TRADE_MIN = 0.002
LIVE_PER_TRADE_MAX = 0.005
LIVE_PER_TRADE_DEFAULT = 0.005

_DONE_STATUSES = frozenset(
    {
        "filled",
        "submitted",
        "rejected",
        "size_invalid",
        "exchange_rejected",
        "skipped",
    }
)

_BASE_ALIASES = {
    "BTC": ("XBT", "XXBT"),
    "XBT": ("BTC", "XXBT"),
}
_QUOTE_ALIASES = {
    "USD": ("ZUSD",),
    "EUR": ("ZEUR",),
}


class ExchangeCallError(RuntimeError):
    """Private-call failure whose message has already had secrets removed."""


def assert_live_per_trade_pct(per_trade_pct: float) -> None:
    """Live trial size is 0.2%–0.5% of equity. Paper's 1% is not accepted."""
    if per_trade_pct < LIVE_PER_TRADE_MIN - 1e-12 or per_trade_pct > LIVE_PER_TRADE_MAX + 1e-12:
        raise ValueError(
            "live per-trade size must be between "
            f"{LIVE_PER_TRADE_MIN:g} and {LIVE_PER_TRADE_MAX:g} "
            f"(0.2%–0.5% of equity); got {per_trade_pct:g}"
        )


def kraken_userref(client_order_id: str) -> int:
    """31-bit Kraken ``userref`` derived from our string ``client_order_id``.

    Kraken's ``cl_ord_id`` accepts a UUID or at most 18 characters of free text.
    Signal ids are longer than that, so they stay in the local SQLite book.
    ``userref`` is an int32 and is not unique on the venue; the txid is.
    """
    digest = hashlib.sha256(client_order_id.encode("utf-8")).digest()
    value = int.from_bytes(digest[:4], "big") & 0x7FFFFFFF
    return value or 1


def parse_spot_balance(balance: dict[str, Any], symbol: str) -> tuple[float, float]:
    """Return ``(quote_total, base_total)`` from a ccxt unified balance.

    Prefers the unified currency code over Kraken aliases (``ZUSD``, ``XXBT``)
    so the same funds are not counted twice.
    """
    if "/" not in symbol:
        raise ValueError(f"live symbol must look like BTC/USD, got {symbol!r}")
    base, quote = symbol.split("/", 1)
    quote_total = _currency_total(balance, (quote, *_QUOTE_ALIASES.get(quote, ())))
    base_total = _currency_total(balance, (base, *_BASE_ALIASES.get(base, ())))
    return quote_total, base_total


def _currency_total(balance: dict[str, Any], codes: tuple[str, ...]) -> float:
    for code in codes:
        bucket = balance.get(code)
        if isinstance(bucket, dict) and bucket.get("total") not in (None, ""):
            return float(bucket["total"])
        totals = balance.get("total")
        if isinstance(totals, dict) and totals.get(code) not in (None, ""):
            return float(totals[code])
    return 0.0


def _redact(text: str, secrets: tuple[str, ...]) -> str:
    cleaned = text
    for secret in secrets:
        if secret:
            cleaned = cleaned.replace(secret, "[redacted]")
    return cleaned[:400]


def _is_withdraw(name: str) -> bool:
    return "withdraw" in name.lower()


class TradeOnlyClient:
    """ccxt Kraken wrapper that places and queries orders and refuses withdrawals."""

    def __init__(self, raw: Any, *, secrets: tuple[str, ...] = ()) -> None:
        self._raw = raw
        self._secrets = tuple(secret for secret in secrets if secret)

    def __repr__(self) -> str:
        return "TradeOnlyClient(kraken, trade-only)"

    def fetch_balance(self) -> dict[str, Any]:
        return self._call(self._raw.fetch_balance)

    def fetch_order(self, order_id: str, symbol: str | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._call(self._raw.fetch_order, order_id, symbol, params or {})

    def create_order(
        self,
        symbol: str,
        type: str,
        side: str,
        amount: float,
        price: float | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if type not in ("market", "limit"):
            raise RuntimeError(f"unsupported live order type {type!r}")
        if side not in ("buy", "sell"):
            raise RuntimeError(f"unsupported live order side {side!r}")
        extra = dict(params or {})
        for key, value in extra.items():
            if _is_withdraw(str(key)) or _is_withdraw(str(value)):
                raise RuntimeError("live client refuses withdraw parameters")
        return self._call(self._raw.create_order, symbol, type, side, amount, price, extra)

    def __getattr__(self, name: str) -> Any:
        if _is_withdraw(name):
            raise RuntimeError(f"live client refuses {name}: trade-only, no withdraw")
        return getattr(self._raw, name)

    def _call(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            message = _redact(f"{type(exc).__name__}: {exc}", self._secrets)
            raise ExchangeCallError(message) from None


def kraken_trade_client(api_key: str, api_secret: str) -> TradeOnlyClient:
    """Build a rate-limited Kraken client. Does not enable withdrawals."""
    import ccxt

    raw = ccxt.kraken(
        {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "timeout": 20_000,
        }
    )
    return TradeOnlyClient(raw, secrets=(api_key, api_secret))


class LiveBroker(PaperBroker):
    """Risk-gated Kraken orders. Unconfigured instances never call the exchange."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        store_path: str | Any = ":memory:",
        enable_trading: bool = False,
        client: Any | None = None,
        market: MarketMetadata | None = None,
        symbol: str = "BTC/USD",
    ) -> None:
        self.armed = False
        self.orders_sent = 0
        self.client = None
        self.market = market if market is not None else StaticMarketMetadata()
        self.symbol = symbol
        self.market_constraints: dict[str, MarketConstraints] = {}
        if not enable_trading or client is None:
            return
        if "/" not in symbol:
            raise ValueError(f"live symbol must look like BTC/USD, got {symbol!r}")
        self.settings = settings or Settings()
        assert_live_per_trade_pct(self.settings.per_trade_pct)
        quote, base_qty, mark = self._read_account(client, symbol, self.market)
        equity = quote + base_qty * (mark or 0.0)
        if equity <= 0:
            raise RuntimeError("Kraken equity is zero; refusing to start live")
        super().__init__(self.settings, store_path=store_path, starting_equity=equity)
        self.client = client
        self._apply_balances(quote, base_qty, mark)
        self.store.set_meta("mode", "live")
        self.store.set_meta("symbol", symbol)
        self.store.commit()
        self.armed = True

    def submit(self, signal: Signal, bar: Bar) -> ExecutionReport:
        if not self.armed or self.client is None:
            raise RuntimeError(
                "Live execution is disabled until quanttrading live is used. "
                "No exchange API keys are loaded by design."
            )
        signal = Signal.model_validate(signal.model_dump())
        existing = self.store.latest_status(signal.client_order_id)
        if existing in _DONE_STATUSES:
            return ExecutionReport(
                status="duplicate",
                client_order_id=signal.client_order_id,
                reason=existing,
                equity=self.equity(),
            )

        self.marks[bar.symbol] = bar.close
        self.risk.on_mark(bar.ts, self.equity())
        working = self._prepare(signal)
        notional = estimated_notional(working, bar)
        decision = self.risk.evaluate(intent=working.intent, notional=notional, equity=self.equity())
        ts = bar.ts.isoformat().replace("+00:00", "Z")
        if not decision.allowed:
            qty_base = _estimate_base(working, bar)
            self._record(
                working,
                ts,
                status="rejected",
                reason=decision.reason,
                qty_base=qty_base,
                price=bar.close,
                notional=notional,
            )
            self.store.save_risk_payload(self.risk.snapshot())
            self.store.commit()
            return ExecutionReport(
                status="rejected",
                client_order_id=working.client_order_id,
                reason=decision.reason,
                qty_base=qty_base,
                notional=notional,
                equity=self.equity(),
            )

        if working.side == "flat" or working.qty <= 0:
            self._record(
                working,
                ts,
                status="skipped",
                reason="no_position",
                qty_base=0.0,
                price=bar.close,
                notional=0.0,
            )
            self.store.commit()
            return ExecutionReport(
                status="skipped",
                client_order_id=working.client_order_id,
                reason="no_position",
                equity=self.equity(),
            )

        constraints = self.market.for_symbol(working.symbol)
        self.market_constraints[working.symbol] = constraints
        order, size_reason = build_would_be_order(working, bar, constraints)
        if size_reason is not None:
            self._record(
                working,
                ts,
                status="size_invalid",
                reason=size_reason,
                qty_base=order.qty,
                price=order.reference_price,
                notional=order.notional,
                order_type=order.order_type,
                limit_price=order.limit_price,
            )
            self.store.commit()
            return ExecutionReport(
                status="size_invalid",
                client_order_id=working.client_order_id,
                reason=size_reason,
                qty_base=order.qty,
                notional=order.notional,
                equity=self.equity(),
            )

        qty, limit_price = self._quantize(order.symbol, order.qty, order.limit_price)
        if working.intent == "open":
            cap = self.equity() * self.risk.limits.per_trade_pct
            if qty * order.reference_price > cap + 1e-6:
                self._record(
                    working,
                    ts,
                    status="rejected",
                    reason="per_trade_limit",
                    qty_base=qty,
                    price=order.reference_price,
                    notional=qty * order.reference_price,
                    order_type=order.order_type,
                    limit_price=limit_price,
                )
                self.store.commit()
                return ExecutionReport(
                    status="rejected",
                    client_order_id=working.client_order_id,
                    reason="per_trade_limit",
                    qty_base=qty,
                    notional=qty * order.reference_price,
                    equity=self.equity(),
                )
        if qty <= 1e-16 or (constraints.min_qty > 0 and qty + 1e-12 < constraints.min_qty):
            self._record(
                working,
                ts,
                status="size_invalid",
                reason="below_min_qty",
                qty_base=qty,
                price=order.reference_price,
                notional=qty * order.reference_price,
                order_type=order.order_type,
                limit_price=limit_price,
            )
            self.store.commit()
            return ExecutionReport(
                status="size_invalid",
                client_order_id=working.client_order_id,
                reason="below_min_qty",
                qty_base=qty,
                equity=self.equity(),
            )

        params = {"userref": kraken_userref(working.client_order_id)}
        try:
            response = self.client.create_order(
                order.symbol,
                order.order_type,
                order.side,
                qty,
                limit_price,
                params,
            )
        except Exception as exc:
            reason = _exchange_reason(exc)
            self._record(
                working,
                ts,
                status="exchange_rejected",
                reason=reason,
                qty_base=qty,
                price=order.reference_price,
                notional=qty * order.reference_price,
                order_type=order.order_type,
                limit_price=limit_price,
            )
            self.store.commit()
            return ExecutionReport(
                status="exchange_rejected",
                client_order_id=working.client_order_id,
                reason=reason,
                qty_base=qty,
                notional=qty * order.reference_price,
                equity=self.equity(),
            )

        self.orders_sent += 1
        if not isinstance(response, dict):
            response = {"id": None, "status": "open"}
        resolved = self._resolve_fill(response, order.symbol)
        exchange_id = _clean_id(resolved.get("id"))
        filled_qty = _as_float(resolved.get("filled")) or 0.0
        average = _as_float(resolved.get("average"))
        if average is None or average <= 0:
            average = _as_float(resolved.get("price"))
        if average is None or average <= 0:
            cost = _as_float(resolved.get("cost"))
            if cost is not None and filled_qty > 0:
                average = cost / filled_qty
        venue_status = str(resolved.get("status") or "")
        if filled_qty > 0 and average is not None and average > 0:
            fill = Fill(
                client_order_id=working.client_order_id,
                ts=ts,
                symbol=order.symbol,
                side=order.side,
                intent=working.intent,
                qty_base=filled_qty,
                price=average,
                notional=filled_qty * average,
                slippage_bps=0.0,
            )
            self._apply_fill(fill)
            self._record(
                working,
                ts,
                status="filled",
                reason=None,
                qty_base=fill.qty_base,
                price=fill.price,
                notional=fill.notional,
                order_type=order.order_type,
                limit_price=limit_price,
                side=fill.side,
                exchange_order_id=exchange_id,
            )
            eq = self.mark_to_market(bar)
            return ExecutionReport(
                status="filled",
                client_order_id=working.client_order_id,
                fill_price=fill.price,
                qty_base=fill.qty_base,
                notional=fill.notional,
                equity=eq,
            )

        if venue_status in {"canceled", "cancelled", "rejected", "expired"}:
            self._record(
                working,
                ts,
                status="exchange_rejected",
                reason=venue_status,
                qty_base=qty,
                price=order.reference_price,
                notional=qty * order.reference_price,
                order_type=order.order_type,
                limit_price=limit_price,
                exchange_order_id=exchange_id,
            )
            self.store.commit()
            return ExecutionReport(
                status="exchange_rejected",
                client_order_id=working.client_order_id,
                reason=venue_status,
                qty_base=qty,
                equity=self.equity(),
            )

        self._record(
            working,
            ts,
            status="submitted",
            reason=venue_status or "accepted",
            qty_base=qty,
            price=order.reference_price,
            notional=qty * order.reference_price,
            order_type=order.order_type,
            limit_price=limit_price,
            side=order.side,
            exchange_order_id=exchange_id,
        )
        self.store.commit()
        return ExecutionReport(
            status="submitted",
            client_order_id=working.client_order_id,
            qty_base=qty,
            notional=qty * order.reference_price,
            equity=self.equity(),
            reason=venue_status or "accepted",
        )

    def mark_to_market(self, bar: Bar) -> float:
        if not self.armed:
            raise RuntimeError("Live execution is disabled in this MVP.")
        return super().mark_to_market(bar)

    def equity(self) -> float:
        if not self.armed:
            raise RuntimeError("Live execution is disabled in this MVP.")
        return super().equity()

    def position_qty(self, symbol: str) -> float:
        if not self.armed:
            raise RuntimeError("Live execution is disabled in this MVP.")
        return super().position_qty(symbol)

    def _read_account(
        self,
        client: Any,
        symbol: str,
        market: MarketMetadata,
    ) -> tuple[float, float, float | None]:
        try:
            balance = client.fetch_balance()
        except ExchangeCallError as exc:
            raise RuntimeError(f"could not read Kraken balance: {exc}") from None
        except Exception as exc:
            raise RuntimeError(f"could not read Kraken balance ({type(exc).__name__})") from None
        if not isinstance(balance, dict):
            raise RuntimeError("could not read Kraken balance (unexpected payload)")
        quote, base_qty = parse_spot_balance(balance, symbol)
        if quote < 0 or base_qty < 0:
            raise RuntimeError("could not read Kraken balance (negative total)")
        mark: float | None = None
        if base_qty > 1e-12:
            constraints = market.for_symbol(symbol)
            last = constraints.last_price
            if last is None or last <= 0:
                raise RuntimeError(f"need a public last price to mark open {symbol}")
            mark = float(last)
        return quote, base_qty, mark

    def _apply_balances(self, quote: float, base_qty: float, mark: float | None) -> None:
        symbol = self.symbol
        previous = self.positions.get(symbol)
        for other in list(self.positions):
            if other != symbol:
                self.positions.pop(other, None)
                self.store.save_position(other, 0.0, 0.0)
        self.cash = quote
        self.store.save_cash(quote)
        if base_qty <= 1e-12:
            self.positions.pop(symbol, None)
            self.store.save_position(symbol, 0.0, 0.0)
            return
        if mark is None or mark <= 0:
            raise RuntimeError(f"need a public last price to mark open {symbol}")
        same = (
            previous is not None
            and abs(previous.qty - base_qty) <= max(1e-8, abs(base_qty) * 1e-4)
            and previous.avg_price > 0
        )
        avg = previous.avg_price if same else mark
        self.positions[symbol] = Position(qty=base_qty, avg_price=avg)
        self.marks[symbol] = mark
        self.store.save_position(symbol, base_qty, avg)

    def _quantize(
        self,
        symbol: str,
        qty: float,
        limit_price: float | None,
    ) -> tuple[float, float | None]:
        amount_fn = getattr(self.client, "amount_to_precision", None)
        if callable(amount_fn):
            qty = float(amount_fn(symbol, qty))
        if limit_price is not None:
            price_fn = getattr(self.client, "price_to_precision", None)
            if callable(price_fn):
                limit_price = float(price_fn(symbol, limit_price))
        return qty, limit_price

    def _resolve_fill(self, response: dict[str, Any], symbol: str) -> dict[str, Any]:
        filled = _as_float(response.get("filled")) or 0.0
        average = _as_float(response.get("average")) or _as_float(response.get("price"))
        if filled > 0 and average is not None and average > 0:
            return response
        order_id = _clean_id(response.get("id"))
        fetch = getattr(self.client, "fetch_order", None)
        if not order_id or not callable(fetch):
            return response
        try:
            fetched = fetch(order_id, symbol)
        except Exception:
            return response
        if not isinstance(fetched, dict):
            return response
        merged = dict(response)
        for key, value in fetched.items():
            if value is not None:
                merged[key] = value
        if not merged.get("id"):
            merged["id"] = order_id
        return merged

    def _record(
        self,
        signal: Signal,
        ts: str,
        *,
        status: str,
        reason: str | None,
        qty_base: float,
        price: float,
        notional: float,
        order_type: str | None = None,
        limit_price: float | None = None,
        side: str | None = None,
        exchange_order_id: str | None = None,
    ) -> None:
        self.store.record_event(
            client_order_id=signal.client_order_id,
            ts=ts,
            symbol=signal.symbol,
            side=side or signal.side,
            intent=signal.intent,
            qty_base=qty_base,
            price=price,
            notional=notional,
            slippage_bps=0.0,
            status=status,
            reason=reason,
            order_type=order_type or signal.order_type,
            limit_price=limit_price if limit_price is not None else signal.limit_price,
            exchange_order_id=exchange_order_id,
        )


def _estimate_base(signal: Signal, bar: Bar) -> float:
    price = signal.limit_price if signal.order_type == "limit" and signal.limit_price else market_anchor_price(signal, bar)
    if price <= 0 or signal.qty <= 0:
        return 0.0
    try:
        return to_base(signal.qty, signal.qty_unit, price)
    except ValueError:
        return 0.0


def _as_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _clean_id(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _exchange_reason(exc: BaseException) -> str:
    if isinstance(exc, ExchangeCallError):
        return str(exc)[:400]
    return f"{type(exc).__name__}: {exc}"[:400]


def _context(broker: LiveBroker, symbol: str) -> StrategyContext:
    util = broker.risk.utilization(broker.equity())
    return StrategyContext(
        equity=broker.equity(),
        position_qty=broker.position_qty(symbol),
        per_trade_pct=broker.risk.limits.per_trade_pct,
        per_trade_util=util.per_trade_pct,
        daily_dd_util=util.daily_dd_pct,
        total_dd_util=util.total_dd_pct,
        halted=broker.risk.halted,
    )


def run_live_latest(bars: list[Bar], strategy: Strategy, broker: LiveBroker) -> ExecutionReport | None:
    """Warm indicators on every bar. Submit only the latest bar's signal.

    Historical signals are discarded. The position used for that decision is
    the synced exchange position, not a replay of old fills.
    """
    if not broker.armed:
        raise RuntimeError("Live execution is disabled in this MVP.")
    if strategy.strategy_id != LIVE_STRATEGY_ID:
        raise ValueError(f"live orders are enabled for {LIVE_STRATEGY_ID} only")
    if not bars:
        raise ValueError("live requires at least one bar")
    symbols = {bar.symbol for bar in bars}
    if symbols != {broker.symbol}:
        raise ValueError(f"live bars must all be {broker.symbol}")
    broker.store.set_meta("strategy_id", strategy.strategy_id)
    broker.store.set_meta("symbol", broker.symbol)
    broker.store.commit()
    for bar in bars[:-1]:
        strategy.on_bar(bar, _context(broker, bar.symbol))
    last = bars[-1]
    broker.mark_to_market(last)
    signal = strategy.on_bar(last, _context(broker, last.symbol))
    report: ExecutionReport | None = None
    if signal is not None:
        util = broker.risk.utilization(broker.equity())
        signal = signal.model_copy(update={"risk": util})
        report = broker.submit(signal, last)
    broker.mark_to_market(last)
    return report


def summarize_live(
    broker: LiveBroker,
    *,
    strategy_id: str,
    n_bars: int,
    last_bar_ts: str,
    report: ExecutionReport | None,
) -> dict[str, object]:
    rows = list(broker.store.fills())

    def _count(status: str) -> int:
        return sum(1 for row in rows if row["status"] == status)

    last_decision: dict[str, object] | None = None
    if report is not None:
        exchange_order_id = None
        for row in reversed(rows):
            if str(row["client_order_id"]) == report.client_order_id:
                exchange_order_id = row["exchange_order_id"]
                break
        last_decision = {
            "status": report.status,
            "reason": report.reason,
            "client_order_id": report.client_order_id,
            "qty_base": report.qty_base,
            "fill_price": report.fill_price,
            "exchange_order_id": exchange_order_id,
        }
    return {
        "mode": "live",
        "strategy_id": strategy_id,
        "symbol": broker.symbol,
        "per_trade_pct": broker.risk.limits.per_trade_pct,
        "orders_sent": broker.orders_sent,
        "n_bars": n_bars,
        "last_bar_ts": last_bar_ts,
        "last_bar_only": True,
        "last_decision": last_decision,
        "n_filled": _count("filled"),
        "n_submitted": _count("submitted"),
        "n_risk_rejected": _count("rejected"),
        "n_size_invalid": _count("size_invalid"),
        "n_exchange_rejected": _count("exchange_rejected"),
        "n_skipped": _count("skipped"),
        "halted": int(broker.risk.halted),
        "halt_reason": broker.risk.halt_reason or "",
        "equity": round(broker.equity(), 4),
        "state_mode": broker.store.get_meta("mode"),
    }
