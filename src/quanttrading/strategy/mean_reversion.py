from __future__ import annotations

from collections import deque
from datetime import timezone
from statistics import fmean, pstdev
from typing import Any

from quanttrading.market import Bar
from quanttrading.signals import Intent, QtyUnit, RiskUtilization, Side, Signal
from quanttrading.strategy.base import Strategy, StrategyContext

MEAN_REVERSION_STRATEGY_ID = "mean_reversion_v1"
DEFAULT_LOOKBACK = 20
DEFAULT_ENTRY_Z = 1.5
DEFAULT_EXIT_Z = 0.0
DEFAULT_SLIPPAGE_BPS = 5.0


def close_zscore(closes: list[float], lookback: int) -> tuple[float, float, float] | None:
    """Mean, population std, and z-score of the latest close versus the lookback window.

    Needs ``lookback`` closes (``lookback >= 2``). Returns ``None`` until that
    warm-up is met. A flat window (std == 0) has z == 0: price is on the mean,
    so it is not a displacement.
    """
    if lookback < 2 or len(closes) < lookback:
        return None
    sample = closes[-lookback:]
    mean = fmean(sample)
    sigma = pstdev(sample)
    z = 0.0 if sigma == 0.0 else (sample[-1] - mean) / sigma
    return mean, sigma, z


class MeanReversion(Strategy):
    """Long-only short-window mean reversion for paper comparison with ``sma_cross_v2``.

    Deterministic given the same bar sequence and ``StrategyContext``. At most
    one Signal per bar. Market orders only. Opens are sized at
    ``equity * per_trade_pct`` in quote; closes flatten the base position.
    Risk hard-stops (1% / 3% / 20%) stay in the execution layer.

    Rules
    -----
    * Warm-up: no entry or exit until ``lookback`` closes exist. Halt-flatten
      does not wait for warm-up.
    * While flat, z-score of the close vs the lookback SMA ``<= -entry_z``
      → market buy, intent=open.
    * While long, z ``>= exit_z`` (default 0, back at the mean) → market sell,
      intent=close, qty = current position.
    * Above-mean prices do not open a short. A second open is not emitted
      while already long.
    * If ``ctx.halted`` and long → flatten (close). Otherwise no signal.

    No min-vol filter. The SMA helper skips *quiet* opens; this strategy's
    entry *is* the displacement, so that filter is not reused.
    """

    def __init__(
        self,
        lookback: int = DEFAULT_LOOKBACK,
        *,
        entry_z: float = DEFAULT_ENTRY_Z,
        exit_z: float = DEFAULT_EXIT_Z,
        strategy_id: str = MEAN_REVERSION_STRATEGY_ID,
        max_slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    ) -> None:
        if lookback < 2:
            raise ValueError("lookback must be >= 2")
        if entry_z <= 0:
            raise ValueError("entry_z must be > 0")
        if exit_z <= -entry_z:
            raise ValueError("exit_z must be > -entry_z")
        if max_slippage_bps < 0:
            raise ValueError("max_slippage_bps must be >= 0")
        if not strategy_id:
            raise ValueError("strategy_id must be non-empty")
        self.lookback = lookback
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.strategy_id = strategy_id
        self.max_slippage_bps = max_slippage_bps
        self._closes: deque[float] = deque(maxlen=lookback)

    def on_bar(self, bar: Bar, ctx: StrategyContext) -> Signal | None:
        self._closes.append(bar.close)
        long = ctx.position_qty > 1e-12
        stats = close_zscore(list(self._closes), self.lookback)
        extra = self._diagnostics(stats)

        if ctx.halted:
            if long:
                return self._signal(
                    bar,
                    ctx,
                    side="sell",
                    intent="close",
                    qty=abs(ctx.position_qty),
                    qty_unit="base",
                    extra=extra,
                )
            return None

        if stats is None:
            return None
        z = stats[2]

        if z <= -self.entry_z and not long:
            qty_quote = max(ctx.equity * ctx.per_trade_pct, 0.0)
            if qty_quote <= 0:
                return None
            return self._signal(
                bar,
                ctx,
                side="buy",
                intent="open",
                qty=qty_quote,
                qty_unit="quote",
                extra=extra,
            )
        if z >= self.exit_z and long:
            return self._signal(
                bar,
                ctx,
                side="sell",
                intent="close",
                qty=abs(ctx.position_qty),
                qty_unit="base",
                extra=extra,
            )
        return None

    def _diagnostics(self, stats: tuple[float, float, float] | None) -> dict[str, Any]:
        if stats is None:
            return {}
        mean, sigma, z = stats
        return {"mean": mean, "std": sigma, "z": z}

    def _signal(
        self,
        bar: Bar,
        ctx: StrategyContext,
        *,
        side: Side,
        intent: Intent,
        qty: float,
        qty_unit: QtyUnit,
        extra: dict[str, Any] | None = None,
    ) -> Signal:
        meta: dict[str, Any] = {
            "lookback": self.lookback,
            "entry_z": self.entry_z,
            "exit_z": self.exit_z,
            "threshold": -self.entry_z,
            "close": bar.close,
        }
        if extra:
            meta.update(extra)
        return Signal(
            strategy_id=self.strategy_id,
            ts=bar.ts.astimezone(timezone.utc),
            symbol=bar.symbol,
            side=side,
            intent=intent,
            qty=qty,
            qty_unit=qty_unit,
            order_type="market",
            limit_price=None,
            strength=1.0,
            max_slippage_bps=self.max_slippage_bps,
            risk=RiskUtilization(
                per_trade_pct=ctx.per_trade_util,
                daily_dd_pct=ctx.daily_dd_util,
                total_dd_pct=ctx.total_dd_util,
            ),
            client_order_id=self._client_order_id(bar, intent, side),
            meta=meta,
        )

    def _client_order_id(self, bar: Bar, intent: str, side: str) -> str:
        ts = bar.ts.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        sym = bar.symbol.replace("/", "").replace(":", "")
        return f"{self.strategy_id}-{sym}-{ts}-{intent}-{side}"


def mean_reversion_strategy(
    *,
    lookback: int = DEFAULT_LOOKBACK,
    entry_z: float = DEFAULT_ENTRY_Z,
    exit_z: float = DEFAULT_EXIT_Z,
    max_slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
) -> MeanReversion:
    """Factory for the paper/backtest mean-reversion comparison strategy."""
    return MeanReversion(
        lookback=lookback,
        entry_z=entry_z,
        exit_z=exit_z,
        max_slippage_bps=max_slippage_bps,
    )
