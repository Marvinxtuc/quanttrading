"""Closed-bar indicators for paper strategies.

EMA uses the standard SMA seed and smoothing factor ``2 / (period + 1)``.
ATR and ADX use Wilder's smoothing. The first ATR (and the first smoothed
directional moves) are simple averages of the first ``period`` true-range
observations, which needs ``period + 1`` bars. ADX is the Wilder smooth of DX
and is available once ``period`` DX values exist (``2 * period`` bars).
"""

from __future__ import annotations


def ema(values: list[float], period: int) -> float | None:
    """EMA of ``values`` at the last point. ``None`` until ``period`` closes exist."""
    if period < 1 or len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    value = sum(values[:period]) / period
    for price in values[period:]:
        value = alpha * price + (1.0 - alpha) * value
    return value


def wilder_atr_adx(
    highs: list[float],
    lows: list[float],
    closes: list[float],
    period: int,
) -> tuple[float | None, float | None]:
    """Return ``(ATR, ADX)`` at the last bar.

    ATR is ``None`` until ``period + 1`` bars exist. ADX is ``None`` until
    ``2 * period`` bars exist. ATR stays in price units (averaged true range),
    which is what an ATR stop needs. Directional indicators use the same
    averaged scale, so DI is the usual 0–100 reading.
    """
    n = len(closes)
    if period < 1 or n != len(highs) or n != len(lows) or n <= period:
        return None, None

    trs: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, n):
        high = highs[i]
        low = lows[i]
        prev_close = closes[i - 1]
        up = high - highs[i - 1]
        down = lows[i - 1] - low
        trs.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        plus_dm.append(up if up > down and up > 0.0 else 0.0)
        minus_dm.append(down if down > up and down > 0.0 else 0.0)

    atr = sum(trs[:period]) / period
    plus = sum(plus_dm[:period]) / period
    minus = sum(minus_dm[:period]) / period
    dx_values = [_dx(plus, minus)]
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
        plus = (plus * (period - 1) + plus_dm[i]) / period
        minus = (minus * (period - 1) + minus_dm[i]) / period
        dx_values.append(_dx(plus, minus))

    if len(dx_values) < period:
        return atr, None
    adx = sum(dx_values[:period]) / period
    for dx in dx_values[period:]:
        adx = (adx * (period - 1) + dx) / period
    return atr, adx


def _dx(plus: float, minus: float) -> float:
    total = plus + minus
    if total == 0.0:
        return 0.0
    return 100.0 * abs(plus - minus) / total


def prior_window(values: list[float], lookback: int) -> list[float] | None:
    """The ``lookback`` values before the last one. ``None`` until that window exists."""
    if lookback < 1 or len(values) <= lookback:
        return None
    return values[-(lookback + 1) : -1]
