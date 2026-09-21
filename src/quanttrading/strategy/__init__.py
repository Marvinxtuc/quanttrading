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
from quanttrading.strategy.sma import (
    DEFAULT_FAST,
    DEFAULT_MIN_VOL,
    DEFAULT_SLOW,
    DEFAULT_SLIPPAGE_BPS,
    DEFAULT_STRATEGY_ID,
    DEFAULT_VOL_WINDOW,
    DefaultStrategy,
    SMACrossover,
    default_strategy,
    realized_vol,
)

KNOWN_STRATEGY_IDS = (DEFAULT_STRATEGY_ID, MEAN_REVERSION_STRATEGY_ID)


def build_strategy(
    strategy_id: str = DEFAULT_STRATEGY_ID,
    *,
    fast: int = DEFAULT_FAST,
    slow: int = DEFAULT_SLOW,
    vol_window: int = DEFAULT_VOL_WINDOW,
    min_vol: float = DEFAULT_MIN_VOL,
    lookback: int = DEFAULT_LOOKBACK,
    entry_z: float = DEFAULT_ENTRY_Z,
    exit_z: float = DEFAULT_EXIT_Z,
    max_slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
) -> Strategy:
    """Build a paper/backtest strategy by id. Default remains ``sma_cross_v2``."""
    if strategy_id == DEFAULT_STRATEGY_ID:
        return default_strategy(
            fast=fast,
            slow=slow,
            vol_window=vol_window,
            min_vol=min_vol,
            max_slippage_bps=max_slippage_bps,
        )
    if strategy_id == MEAN_REVERSION_STRATEGY_ID:
        return mean_reversion_strategy(
            lookback=lookback,
            entry_z=entry_z,
            exit_z=exit_z,
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
    "KNOWN_STRATEGY_IDS",
    "MEAN_REVERSION_STRATEGY_ID",
    "DefaultStrategy",
    "MeanReversion",
    "SMACrossover",
    "Strategy",
    "StrategyContext",
    "build_strategy",
    "close_zscore",
    "default_strategy",
    "mean_reversion_strategy",
    "realized_vol",
]
