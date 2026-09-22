"""Rolling out-of-sample paper report for 4h paper strategies.

Runs ``trend_breakout_v1`` and ``range_reversion_v2`` separately on long
history with expanding walk-forward folds, a final untouched holdout, and
cost stress at round-trip 0.003 / 0.006. Paper equity still uses slippage
only; fee-adjusted figures are labeled estimates.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from quanttrading.backtest.metrics import max_drawdown
from quanttrading.backtest.runner import run_bars
from quanttrading.config import Settings
from quanttrading.execution.paper import PaperBroker
from quanttrading.market import Bar
from quanttrading.strategy import (
    RANGE_REVERSION_STRATEGY_ID,
    TREND_BREAKOUT_STRATEGY_ID,
    build_strategy,
)

OOS_STRATEGY_IDS = (TREND_BREAKOUT_STRATEGY_ID, RANGE_REVERSION_STRATEGY_ID)
DEFAULT_COST_STRESS = (0.003, 0.006)
DEFAULT_HOLDOUT_FRAC = 0.20
DEFAULT_TEST_FRAC = 0.15
DEFAULT_MIN_TRAIN_FRAC = 0.35
DEFAULT_PER_TRADE_PCT = 0.01


@dataclass(frozen=True)
class FoldWindow:
    """Indices into the full bar list: warm-up uses ``[0, test_end)``."""

    fold_id: str
    kind: str  # "walk_forward" | "holdout"
    train_end: int
    test_start: int
    test_end: int

    def label(self, bars: Sequence[Bar]) -> dict[str, Any]:
        return {
            "fold_id": self.fold_id,
            "kind": self.kind,
            "train_end_idx": self.train_end,
            "test_start_idx": self.test_start,
            "test_end_idx": self.test_end,
            "test_start": _iso(bars[self.test_start].ts),
            "test_end": _iso(bars[self.test_end - 1].ts),
            "n_test_bars": self.test_end - self.test_start,
        }


@dataclass
class RoundTrip:
    entry_ts: str
    exit_ts: str
    entry_price: float
    exit_price: float
    qty_base: float
    entry_notional: float
    pnl: float


@dataclass
class FoldMetrics:
    fold_id: str
    kind: str
    round_trip_cost: float
    n_round_trips: int
    profit_factor: float | None
    max_dd: float
    after_cost_ev_per_rt: float | None
    fee_share_estimate: float | None
    gross_profit: float
    gross_loss: float
    total_fee_estimate: float
    sum_pnl: float
    buy_hold_1pct_pnl: float
    buy_hold_1pct_return_on_sleeve: float
    cash_baseline_pnl: float
    n_fills_in_window: int
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    return ts.isoformat().replace("+00:00", "Z")


def _parse_ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)


def build_expanding_folds(
    n_bars: int,
    *,
    holdout_frac: float = DEFAULT_HOLDOUT_FRAC,
    test_frac: float = DEFAULT_TEST_FRAC,
    min_train_frac: float = DEFAULT_MIN_TRAIN_FRAC,
) -> list[FoldWindow]:
    """Expanding walk-forward folds plus a final untouched holdout.

    Research window is ``bars[:holdout_start]``. Train grows from
    ``min_train``; each fold's test is the next ``test_size`` bars. The
    holdout ``bars[holdout_start:]`` is evaluated once and never used as
    training history for a later fold (params are fixed anyway; the split
    documents an untouched final period).
    """
    if n_bars < 50:
        raise ValueError(f"need at least 50 bars for OOS, got {n_bars}")
    if not (0.05 <= holdout_frac <= 0.4):
        raise ValueError("holdout_frac must be in [0.05, 0.4]")
    if not (0.05 <= test_frac <= 0.4):
        raise ValueError("test_frac must be in [0.05, 0.4]")
    if not (0.2 <= min_train_frac <= 0.8):
        raise ValueError("min_train_frac must be in [0.2, 0.8]")

    holdout_start = int(n_bars * (1.0 - holdout_frac))
    holdout_start = max(holdout_start, 1)
    research_n = holdout_start
    test_size = max(int(research_n * test_frac), 1)
    min_train = max(int(research_n * min_train_frac), 1)
    if min_train + test_size > research_n:
        # Fall back to a single research fold + holdout.
        folds = [
            FoldWindow(
                fold_id="wf_0",
                kind="walk_forward",
                train_end=max(research_n // 2, 1),
                test_start=max(research_n // 2, 1),
                test_end=research_n,
            )
        ]
    else:
        folds = []
        train_end = min_train
        fold_i = 0
        while train_end + test_size <= research_n:
            test_end = train_end + test_size
            folds.append(
                FoldWindow(
                    fold_id=f"wf_{fold_i}",
                    kind="walk_forward",
                    train_end=train_end,
                    test_start=train_end,
                    test_end=test_end,
                )
            )
            fold_i += 1
            train_end = test_end
        # Leftover research bars (shorter than a full test) attach to the last fold.
        if folds and folds[-1].test_end < research_n:
            last = folds[-1]
            folds[-1] = FoldWindow(
                fold_id=last.fold_id,
                kind=last.kind,
                train_end=last.train_end,
                test_start=last.test_start,
                test_end=research_n,
            )

    if holdout_start < n_bars:
        folds.append(
            FoldWindow(
                fold_id="holdout",
                kind="holdout",
                train_end=holdout_start,
                test_start=holdout_start,
                test_end=n_bars,
            )
        )
    return folds


def pair_round_trips(fills: Sequence[Any]) -> list[RoundTrip]:
    """Pair filled open buys with the next filled close/sell into round trips."""
    trips: list[RoundTrip] = []
    open_fill: Any | None = None
    for row in fills:
        if str(row["status"]) != "filled":
            continue
        intent = str(row["intent"])
        side = str(row["side"])
        if intent == "open" and side == "buy":
            open_fill = row
            continue
        if open_fill is None:
            continue
        if intent in ("close", "reduce") and side == "sell":
            entry_px = float(open_fill["price"])
            exit_px = float(row["price"])
            qty = min(float(open_fill["qty_base"]), float(row["qty_base"]))
            pnl = (exit_px - entry_px) * qty
            trips.append(
                RoundTrip(
                    entry_ts=str(open_fill["ts"]),
                    exit_ts=str(row["ts"]),
                    entry_price=entry_px,
                    exit_price=exit_px,
                    qty_base=qty,
                    entry_notional=float(open_fill["notional"]),
                    pnl=pnl,
                )
            )
            open_fill = None
    return trips


def _equity_in_window(curve: Sequence[tuple[str, float]], start: datetime, end: datetime) -> list[float]:
    values: list[float] = []
    for ts, eq in curve:
        t = _parse_ts(ts)
        if start <= t <= end:
            values.append(float(eq))
    return values


def buy_and_hold_1pct(
    bars: Sequence[Bar],
    *,
    test_start: int,
    test_end: int,
    equity: float,
    per_trade_pct: float = DEFAULT_PER_TRADE_PCT,
) -> tuple[float, float]:
    """Long once at test start with ``equity * per_trade_pct`` notionals; hold to test end.

    Returns ``(pnl_usd, return_on_sleeve)`` where the sleeve is the 1% notional.
    """
    if test_end <= test_start + 1:
        return 0.0, 0.0
    entry = bars[test_start]
    exit_bar = bars[test_end - 1]
    notional = equity * per_trade_pct
    if entry.open <= 0 or notional <= 0:
        return 0.0, 0.0
    qty = notional / entry.open
    pnl = qty * (exit_bar.close - entry.open)
    return pnl, pnl / notional


def fold_metrics_from_broker(
    broker: PaperBroker,
    fold: FoldWindow,
    bars: Sequence[Bar],
    *,
    round_trip_cost: float,
    starting_equity: float,
) -> FoldMetrics:
    """Score one fold using fills whose exit falls in the test window."""
    test_start_ts = bars[fold.test_start].ts
    test_end_ts = bars[fold.test_end - 1].ts
    fills = list(broker.store.fills())
    trips_all = pair_round_trips(fills)
    trips = [t for t in trips_all if test_start_ts <= _parse_ts(t.exit_ts) <= test_end_ts]
    fills_in_window = [
        row
        for row in fills
        if str(row["status"]) == "filled" and test_start_ts <= _parse_ts(str(row["ts"])) <= test_end_ts
    ]

    pnls = [t.pnl for t in trips]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    if gross_loss > 0:
        profit_factor: float | None = round(gross_profit / gross_loss, 6)
    elif gross_profit > 0:
        profit_factor = None
    else:
        profit_factor = 0.0

    fee_estimates = [round_trip_cost * t.entry_notional for t in trips]
    total_fee = sum(fee_estimates)
    after_cost = [t.pnl - fee for t, fee in zip(trips, fee_estimates, strict=True)]
    if trips:
        ev = sum(after_cost) / len(trips)
    else:
        ev = None

    if gross_profit > 1e-12:
        fee_share: float | None = total_fee / gross_profit
    else:
        fee_share = None

    curve = broker.store.equity_curve()
    eq_window = _equity_in_window(curve, test_start_ts, test_end_ts)
    if not eq_window:
        eq_window = [starting_equity]
    dd = max_drawdown(eq_window)

    bh_pnl, bh_ret = buy_and_hold_1pct(
        bars,
        test_start=fold.test_start,
        test_end=fold.test_end,
        equity=starting_equity,
    )

    notes: list[str] = [
        "Paper equity path applies slippage only; fees are not deducted from cash.",
        (
            f"Fee-adjusted EV uses labeled estimate: "
            f"pnl_i - ({round_trip_cost:g} * entry_notional_i) per completed round trip."
        ),
    ]
    if not trips:
        notes.append("No completed round trips with exit in this test window.")

    return FoldMetrics(
        fold_id=fold.fold_id,
        kind=fold.kind,
        round_trip_cost=round_trip_cost,
        n_round_trips=len(trips),
        profit_factor=profit_factor,
        max_dd=round(dd, 6),
        after_cost_ev_per_rt=None if ev is None else round(ev, 6),
        fee_share_estimate=None if fee_share is None else round(fee_share, 6),
        gross_profit=round(gross_profit, 6),
        gross_loss=round(gross_loss, 6),
        total_fee_estimate=round(total_fee, 6),
        sum_pnl=round(sum(pnls), 6),
        buy_hold_1pct_pnl=round(bh_pnl, 6),
        buy_hold_1pct_return_on_sleeve=round(bh_ret, 6),
        cash_baseline_pnl=0.0,
        n_fills_in_window=len(fills_in_window),
        notes=notes,
    )


def run_fold(
    bars: Sequence[Bar],
    fold: FoldWindow,
    *,
    strategy_id: str,
    round_trip_cost: float,
    settings: Settings | None = None,
) -> FoldMetrics:
    """Replay from bar 0 through ``fold.test_end``; score the test window only."""
    if strategy_id not in OOS_STRATEGY_IDS:
        raise ValueError(f"OOS report supports {OOS_STRATEGY_IDS}, got {strategy_id!r}")
    settings = settings or Settings()
    slice_bars = list(bars[: fold.test_end])
    broker = PaperBroker(settings, store_path=":memory:")
    strategy = build_strategy(strategy_id, round_trip_cost=round_trip_cost)
    run_bars(slice_bars, strategy, broker)
    return fold_metrics_from_broker(
        broker,
        fold,
        bars,
        round_trip_cost=round_trip_cost,
        starting_equity=float(settings.paper_equity),
    )


def aggregate_folds(folds: Iterable[FoldMetrics]) -> dict[str, Any]:
    rows = list(folds)
    n = len(rows)
    trips = sum(r.n_round_trips for r in rows)
    after = [r.after_cost_ev_per_rt for r in rows if r.after_cost_ev_per_rt is not None and r.n_round_trips > 0]
    # Weight EV by trade count across folds that traded.
    weighted_num = 0.0
    weighted_den = 0
    for r in rows:
        if r.after_cost_ev_per_rt is not None and r.n_round_trips > 0:
            weighted_num += r.after_cost_ev_per_rt * r.n_round_trips
            weighted_den += r.n_round_trips
    pf_num = sum(r.gross_profit for r in rows)
    pf_den = sum(r.gross_loss for r in rows)
    if pf_den > 0:
        pf: float | None = round(pf_num / pf_den, 6)
    elif pf_num > 0:
        pf = None
    else:
        pf = 0.0
    fee_total = sum(r.total_fee_estimate for r in rows)
    fee_share = round(fee_total / pf_num, 6) if pf_num > 1e-12 else None
    return {
        "fold_count": n,
        "n_round_trips": trips,
        "after_cost_ev_per_rt_trade_weighted": None
        if weighted_den == 0
        else round(weighted_num / weighted_den, 6),
        "after_cost_ev_per_rt_fold_mean": None if not after else round(sum(after) / len(after), 6),
        "profit_factor": pf,
        "max_dd_worst_fold": max((r.max_dd for r in rows), default=0.0),
        "total_fee_estimate": round(fee_total, 6),
        "fee_share_estimate": fee_share,
        "sum_pnl": round(sum(r.sum_pnl for r in rows), 6),
        "buy_hold_1pct_pnl_sum": round(sum(r.buy_hold_1pct_pnl for r in rows), 6),
        "cash_baseline_pnl": 0.0,
    }


def run_strategy_costs(
    bars: Sequence[Bar],
    folds: Sequence[FoldWindow],
    *,
    strategy_id: str,
    costs: Sequence[float],
    settings: Settings,
) -> dict[str, Any]:
    """One causal replay per cost through the full series; score each fold window.

    Because the strategy and risk gate are causal, fills inside a test window
    match an expanding run that stopped at ``test_end``. This avoids replaying
    the same prefix once per fold on multi-year CSVs.
    """
    if strategy_id not in OOS_STRATEGY_IDS:
        raise ValueError(f"OOS report supports {OOS_STRATEGY_IDS}, got {strategy_id!r}")
    by_cost: dict[str, Any] = {}
    full = list(bars)
    for cost in costs:
        broker = PaperBroker(settings, store_path=":memory:")
        strategy = build_strategy(strategy_id, round_trip_cost=cost)
        run_bars(full, strategy, broker)
        fold_rows = [
            fold_metrics_from_broker(
                broker,
                fold,
                bars,
                round_trip_cost=cost,
                starting_equity=float(settings.paper_equity),
            )
            for fold in folds
        ]
        by_cost[f"{cost:g}"] = {
            "round_trip_cost": cost,
            "folds": [row.as_dict() for row in fold_rows],
            "aggregate": aggregate_folds(fold_rows),
        }
    return {
        "fold_windows": [f.label(bars) for f in folds],
        "by_cost": by_cost,
    }


def run_oos_report(
    bars: Sequence[Bar],
    *,
    strategy_ids: Sequence[str] = OOS_STRATEGY_IDS,
    costs: Sequence[float] = DEFAULT_COST_STRESS,
    settings: Settings | None = None,
    holdout_frac: float = DEFAULT_HOLDOUT_FRAC,
    test_frac: float = DEFAULT_TEST_FRAC,
    min_train_frac: float = DEFAULT_MIN_TRAIN_FRAC,
) -> dict[str, Any]:
    """Run the full multi-strategy / multi-cost OOS report."""
    settings = settings or Settings()
    folds = build_expanding_folds(
        len(bars),
        holdout_frac=holdout_frac,
        test_frac=test_frac,
        min_train_frac=min_train_frac,
    )
    span = {
        "n_bars": len(bars),
        "start": _iso(bars[0].ts),
        "end": _iso(bars[-1].ts),
        "days": round((bars[-1].ts - bars[0].ts).total_seconds() / 86_400.0, 2),
        "years": round((bars[-1].ts - bars[0].ts).total_seconds() / (86_400.0 * 365.25), 3),
    }
    caveats: list[str] = [
        "Live sma_cross_v2 is unchanged; this report is paper-only.",
        "Round-trip cost is the strategy entry filter and a labeled fee estimate; "
        "the paper broker does not debit commissions from equity.",
        "Buy-and-hold baseline sizes 1% of starting paper equity once at each fold's test open.",
        "Cash baseline is flat (0 PnL).",
    ]
    if span["years"] < 3.0:
        caveats.append(
            f"Data span is {span['years']} years ({span['days']} days), below the ~3 year target. "
            "OOS still ran on the available history."
        )

    strategies: dict[str, Any] = {}
    for strategy_id in strategy_ids:
        strategies[strategy_id] = run_strategy_costs(
            bars,
            folds,
            strategy_id=strategy_id,
            costs=costs,
            settings=settings,
        )

    return {
        "span": span,
        "settings": {
            "paper_equity": settings.paper_equity,
            "per_trade_pct": settings.per_trade_pct,
            "daily_dd_pct": settings.daily_dd_pct,
            "total_dd_pct": settings.total_dd_pct,
            "max_slippage_bps": settings.max_slippage_bps,
            "holdout_frac": holdout_frac,
            "test_frac": test_frac,
            "min_train_frac": min_train_frac,
            "costs": list(costs),
        },
        "caveats": caveats,
        "strategies": strategies,
    }


def render_oos_markdown(report: dict[str, Any]) -> str:
    """Render a trader-facing markdown summary from ``run_oos_report`` output."""
    span = report["span"]
    lines: list[str] = [
        "# Rolling out-of-sample report (paper only)",
        "",
        f"- Bars: **{span['n_bars']}** 4h candles from `{span['start']}` to `{span['end']}`",
        f"- Span: **{span['days']}** days (**{span['years']}** years)",
        f"- Paper equity: {report['settings']['paper_equity']:g} USD; gates "
        f"{report['settings']['per_trade_pct']:.0%} / "
        f"{report['settings']['daily_dd_pct']:.0%} / "
        f"{report['settings']['total_dd_pct']:.0%}",
        "",
        "## Caveats",
        "",
    ]
    for note in report["caveats"]:
        lines.append(f"- {note}")
    lines.extend(["", "## Results by strategy", ""])

    for strategy_id, block in report["strategies"].items():
        lines.append(f"### `{strategy_id}`")
        lines.append("")
        windows = block["fold_windows"]
        lines.append(f"Fold count (including holdout): **{len(windows)}**")
        lines.append("")
        lines.append("| Fold | Kind | Test start | Test end | Bars |")
        lines.append("| --- | --- | --- | --- | ---: |")
        for w in windows:
            lines.append(
                f"| `{w['fold_id']}` | {w['kind']} | `{w['test_start']}` | `{w['test_end']}` | {w['n_test_bars']} |"
            )
        lines.append("")
        for cost_key, cost_block in block["by_cost"].items():
            agg = cost_block["aggregate"]
            lines.append(f"#### Cost stress `round_trip={cost_key}`")
            lines.append("")
            if agg["n_round_trips"] == 0:
                lines.append(
                    "> No completed round trips under default parameters on this "
                    "history. That is a real (quiet) outcome, not a missing run."
                )
                lines.append("")
            lines.append(
                "| Metric | Value |"
            )
            lines.append("| --- | --- |")
            lines.append(f"| Fold count | {agg['fold_count']} |")
            lines.append(f"| Full round-trip trade count | {agg['n_round_trips']} |")
            lines.append(
                f"| After-cost EV / RT (trade-weighted, **fee estimate**) | "
                f"{agg['after_cost_ev_per_rt_trade_weighted']} |"
            )
            lines.append(f"| Profit factor | {agg['profit_factor']} |")
            lines.append(f"| Max drawdown (worst fold) | {agg['max_dd_worst_fold']} |")
            lines.append(f"| Fee share estimate (fees / gross profit) | {agg['fee_share_estimate']} |")
            lines.append(f"| Sum PnL (slippage path, no fee debit) | {agg['sum_pnl']} |")
            lines.append(f"| Buy-and-hold 1% sleeve PnL (sum of folds) | {agg['buy_hold_1pct_pnl_sum']} |")
            lines.append(f"| Cash baseline PnL | {agg['cash_baseline_pnl']} |")
            lines.append("")
            lines.append("Per-fold detail:")
            lines.append("")
            lines.append(
                "| Fold | RTs | After-cost EV/RT | PF | Max DD | Fee share | BH 1% PnL | Cash |"
            )
            lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
            for row in cost_block["folds"]:
                lines.append(
                    f"| `{row['fold_id']}` | {row['n_round_trips']} | "
                    f"{row['after_cost_ev_per_rt']} | {row['profit_factor']} | "
                    f"{row['max_dd']} | {row['fee_share_estimate']} | "
                    f"{row['buy_hold_1pct_pnl']} | {row['cash_baseline_pnl']} |"
                )
            lines.append("")
    lines.extend(
        [
            "## How to regenerate",
            "",
            "```bash",
            "quanttrading fetch-history --out data/btcusd_4h_long.csv",
            "quanttrading oos-report --data data/btcusd_4h_long.csv --out reports/oos_4h.md",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "DEFAULT_COST_STRESS",
    "DEFAULT_HOLDOUT_FRAC",
    "DEFAULT_MIN_TRAIN_FRAC",
    "DEFAULT_TEST_FRAC",
    "FoldMetrics",
    "FoldWindow",
    "OOS_STRATEGY_IDS",
    "RoundTrip",
    "aggregate_folds",
    "build_expanding_folds",
    "buy_and_hold_1pct",
    "fold_metrics_from_broker",
    "pair_round_trips",
    "render_oos_markdown",
    "run_fold",
    "run_oos_report",
    "run_strategy_costs",
]
