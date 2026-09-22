from quanttrading.strategy.base import Strategy, StrategyContext
from quanttrading.strategy.mean_reversion import (
    DEFAULT_ENTRY_Z,
    DEFAULT_EXIT_Z,
    DEFAULT_LOOKBACK,
    MEAN_REVERSION_STRATEGY_ID,
    MeanReversion,
    close_zscore,
    mean_reversion_strategy,
)
from quanttrading.strategy.range_reversion import (
    DEFAULT_ROUND_TRIP_COST as RANGE_DEFAULT_ROUND_TRIP_COST,
    DEFAULT_STOP_ATR as RANGE_DEFAULT_STOP_ATR,
    RANGE_REVERSION_STRATEGY_ID,
    RangeReversion,
    range_reversion_strategy,
)
from quanttrading.strategy.trend_breakout import (
    DEFAULT_ROUND_TRIP_COST,
    DEFAULT_STOP_ATR,
    TREND_BREAKOUT_STRATEGY_ID,
    TrendBreakout,
    trend_breakout_strategy,
)
from quanttrading.strategy.sma import (
    DEFAULT_FAST,
    DEFAULT_MIN_VOL,
    DEFAULT_SLOW,
    DEFAULT_SLIPPAGE_BPS,
    DEFAULT_STRATEGY_ID,
    DEFAULT_VOL_WINDOW,
    HF_MIN_VOL,
    HF_VOL_WINDOW,
    SMA_CROSS_15M_STRATEGY_ID,
    SMA_CROSS_5M_STRATEGY_ID,
    DefaultStrategy,
    SMACrossover,
    default_strategy,
    realized_vol,
    sma_cross_15m_strategy,
    sma_cross_5m_strategy,
)

KNOWN_STRATEGY_IDS = (
    DEFAULT_STRATEGY_ID,
    SMA_CROSS_15M_STRATEGY_ID,
    SMA_CROSS_5M_STRATEGY_ID,
    MEAN_REVERSION_STRATEGY_ID,
    TREND_BREAKOUT_STRATEGY_ID,
    RANGE_REVERSION_STRATEGY_ID,
)

_HF_STRATEGY_FACTORIES = {
    SMA_CROSS_15M_STRATEGY_ID: sma_cross_15m_strategy,
    SMA_CROSS_5M_STRATEGY_ID: sma_cross_5m_strategy,
}


def build_strategy(
    strategy_id: str = DEFAULT_STRATEGY_ID,
    *,
    fast: int = DEFAULT_FAST,
    slow: int = DEFAULT_SLOW,
    vol_window: int | None = None,
    min_vol: float | None = None,
    lookback: int = DEFAULT_LOOKBACK,
    entry_z: float = DEFAULT_ENTRY_Z,
    exit_z: float = DEFAULT_EXIT_Z,
    max_slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    stop_atr: float | None = None,
    round_trip_cost: float = DEFAULT_ROUND_TRIP_COST,
) -> Strategy:
    """Build a paper/backtest strategy by id. Default remains ``sma_cross_v2``.

    ``stop_atr`` and ``round_trip_cost`` apply to ``trend_breakout_v1`` and
    ``range_reversion_v2`` (each has its own stop default when ``stop_atr`` is
    omitted). Both ids are paper and dry-run only.

    ``vol_window`` and ``min_vol`` default to the selected SMA variant:
    20 / 0.0005 for ``sma_cross_v2``, 16 / 0.0002 for the 15m and 5m variants.
    Pass a number to override that variant's open filter.
    """
    if strategy_id == DEFAULT_STRATEGY_ID:
        return default_strategy(
            fast=fast,
            slow=slow,
            vol_window=DEFAULT_VOL_WINDOW if vol_window is None else vol_window,
            min_vol=DEFAULT_MIN_VOL if min_vol is None else min_vol,
            max_slippage_bps=max_slippage_bps,
        )
    hf_factory = _HF_STRATEGY_FACTORIES.get(strategy_id)
    if hf_factory is not None:
        return hf_factory(
            fast=fast,
            slow=slow,
            vol_window=HF_VOL_WINDOW if vol_window is None else vol_window,
            min_vol=HF_MIN_VOL if min_vol is None else min_vol,
            max_slippage_bps=max_slippage_bps,
        )
    if strategy_id == MEAN_REVERSION_STRATEGY_ID:
        return mean_reversion_strategy(
            lookback=lookback,
            entry_z=entry_z,
            exit_z=exit_z,
            max_slippage_bps=max_slippage_bps,
        )
    if strategy_id == TREND_BREAKOUT_STRATEGY_ID:
        return trend_breakout_strategy(
            stop_atr=DEFAULT_STOP_ATR if stop_atr is None else stop_atr,
            round_trip_cost=round_trip_cost,
            max_slippage_bps=max_slippage_bps,
        )
    if strategy_id == RANGE_REVERSION_STRATEGY_ID:
        return range_reversion_strategy(
            stop_atr=RANGE_DEFAULT_STOP_ATR if stop_atr is None else stop_atr,
            round_trip_cost=round_trip_cost,
            max_slippage_bps=max_slippage_bps,
        )
    known = ", ".join(KNOWN_STRATEGY_IDS)
    raise ValueError(f"unknown strategy {strategy_id!r}; expected one of: {known}")


__all__ = [
    "DEFAULT_ENTRY_Z",
    "DEFAULT_EXIT_Z",
    "DEFAULT_FAST",
    "DEFAULT_LOOKBACK",
    "DEFAULT_MIN_VOL",
    "DEFAULT_SLOW",
    "DEFAULT_SLIPPAGE_BPS",
    "DEFAULT_STRATEGY_ID",
    "DEFAULT_VOL_WINDOW",
    "HF_MIN_VOL",
    "HF_VOL_WINDOW",
    "KNOWN_STRATEGY_IDS",
    "DEFAULT_ROUND_TRIP_COST",
    "DEFAULT_STOP_ATR",
    "RANGE_DEFAULT_ROUND_TRIP_COST",
    "RANGE_DEFAULT_STOP_ATR",
    "MEAN_REVERSION_STRATEGY_ID",
    "RANGE_REVERSION_STRATEGY_ID",
    "SMA_CROSS_15M_STRATEGY_ID",
    "SMA_CROSS_5M_STRATEGY_ID",
    "TREND_BREAKOUT_STRATEGY_ID",
    "DefaultStrategy",
    "MeanReversion",
    "RangeReversion",
    "SMACrossover",
    "Strategy",
    "StrategyContext",
    "TrendBreakout",
    "build_strategy",
    "close_zscore",
    "default_strategy",
    "mean_reversion_strategy",
    "range_reversion_strategy",
    "realized_vol",
    "sma_cross_15m_strategy",
    "sma_cross_5m_strategy",
    "trend_breakout_strategy",
]
