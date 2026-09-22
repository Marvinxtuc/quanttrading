"""Read-only status snapshot from the paper SQLite book and an optional heartbeat."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from quanttrading.backtest.metrics import max_drawdown

_SIDES = frozenset({"buy", "sell", "flat"})
_INTENTS = frozenset({"open", "close", "reduce"})
_TS_TOKEN = re.compile(r"\d{8}T\d{6}Z")
_TIMEFRAME = re.compile(r"^(?P<n>\d+)(?P<u>[smhd])$", re.IGNORECASE)
_MODES = frozenset({"paper", "dry-run"})
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class StatusReadError(Exception):
    """The state database exists but could not be read."""


def timeframe_seconds(timeframe: str) -> int:
    match = _TIMEFRAME.fullmatch(timeframe.strip())
    if match is None:
        raise ValueError(f"unsupported timeframe {timeframe!r} (expected e.g. 1m, 1h, 1d)")
    count = int(match.group("n"))
    if count <= 0:
        raise ValueError(f"unsupported timeframe {timeframe!r}")
    return count * _UNIT_SECONDS[match.group("u").lower()]


def default_heartbeat_path(state: Path) -> Path:
    return state.with_suffix(".heartbeat.json")


def strategy_from_client_order_id(client_order_id: str) -> str | None:
    """Parse `{strategy}-{symbol}-{YYYYMMDDTHHMMSSZ}-{intent}-{side}`."""
    parts = client_order_id.split("-")
    if len(parts) < 5:
        return None
    side, intent, ts = parts[-1], parts[-2], parts[-3]
    if side not in _SIDES or intent not in _INTENTS or _TS_TOKEN.fullmatch(ts) is None:
        return None
    head = parts[:-3]
    if len(head) < 2:
        return None
    strategy = "-".join(head[:-1]).strip()
    return strategy or None


def build_status(
    state: Path | str,
    *,
    heartbeat: Path | str | None = None,
    timeframe: str | None = None,
    now: datetime | None = None,
    fill_limit: int = 100,
) -> dict[str, Any]:
    """Build a JSON-ready status document. Does not create or modify the database."""
    state_path = Path(state)
    if not state_path.is_file():
        raise FileNotFoundError(f"state database not found: {state_path}")

    if heartbeat is not None:
        hb_path = Path(heartbeat)
    else:
        hb_path = default_heartbeat_path(state_path)
    heartbeat_doc, heartbeat_error = _load_heartbeat(hb_path) if hb_path.is_file() else ({}, None)

    clock = _as_utc(now or datetime.now(timezone.utc))
    limit = min(max(int(fill_limit), 1), 200)

    try:
        conn = _connect_ro(state_path)
        try:
            meta = _meta(conn)
            positions = _positions(conn)
            equity_row = _latest_equity(conn)
            curve = _equity_values(conn)
            fills = _fills(conn, limit)
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise StatusReadError(f"could not read state database: {exc}") from exc

    risk = _parse_risk(meta.get("risk"))
    if equity_row is not None:
        equity: float | None = equity_row["equity"]
        snap_cash: float | None = equity_row["cash"]
        cash = snap_cash
    else:
        cash = _float_or_none(meta.get("cash"))
        equity = cash
        snap_cash = cash

    marks = _marks(positions, equity, snap_cash, fills)
    enriched = [
        _enrich_position(row, marks.get(row["symbol"]), equity)
        for row in positions
        if abs(row["qty"]) >= 1e-16
    ]
    symbol = _primary_symbol(enriched, fills) or heartbeat_doc.get("symbol") or _clean(meta.get("symbol"))
    strategy_id = (
        heartbeat_doc.get("strategy_id")
        or _clean(meta.get("strategy_id"))
        or _strategy_from_fills(fills)
    )
    mode = heartbeat_doc.get("mode") or _mode_from_meta(meta) or "paper"
    tf = _resolve_timeframe(timeframe, heartbeat_doc.get("timeframe"), meta.get("timeframe"))
    tf_sec = timeframe_seconds(tf)

    last_bar = heartbeat_doc.get("last_bar_ts") or _optional_ts(meta.get("last_bar_ts"))
    if last_bar is None and equity_row is not None:
        last_bar = _optional_ts(equity_row["ts"])
    if last_bar is None and fills:
        last_bar = _optional_ts(str(fills[0]["ts"]))

    lag: float | None = None
    stale = True
    if last_bar is not None:
        lag = (clock - _parse_ts(last_bar)).total_seconds()
        stale = lag > 2 * tf_sec

    peak = _float_or_none(risk.get("peak_equity")) if risk else None
    current_dd = 0.0
    if equity is not None and peak is not None and peak > 0:
        current_dd = max((peak - equity) / peak, 0.0)
    curve_dd = max_drawdown(curve) if curve else 0.0
    max_dd = round(max(curve_dd, current_dd) * 100.0, 4) if equity is not None else None

    day_start = _float_or_none(risk.get("day_start_equity")) if risk else None
    start_equity = _float_or_none(risk.get("starting_equity")) if risk else None
    if start_equity is None and curve:
        start_equity = curve[0]

    halted = bool(int(risk.get("halted") or 0)) if risk else False
    halt_reason = None
    if halted and risk and risk.get("halt_reason"):
        halt_reason = str(risk["halt_reason"])
    day = str(risk["day"]) if risk and risk.get("day") else None
    daily_halt = str(risk["daily_halt_date"]) if risk and risk.get("daily_halt_date") else None

    return {
        "overview": {
            "strategy_id": strategy_id,
            "mode": mode,
            "symbol": symbol,
            "equity": _round(equity),
            "cash": _round(cash),
            "unrealized_pnl": _round(_unrealized(enriched)),
            "day_pnl_pct": _pct_change(equity, day_start),
            "rolling_pnl_pct": _pct_change(equity, start_equity),
            "max_dd_pct": max_dd,
            "halted": halted,
            "halt_reason": halt_reason,
            "daily_breaker_active": bool(day and daily_halt and day == daily_halt),
        },
        "position": _primary_position(enriched, symbol),
        "positions": enriched,
        "fills": [_fill_view(row) for row in fills],
        "heartbeat": {
            "last_bar_ts": last_bar,
            "lag_sec": None if lag is None else round(lag, 3),
            "timeframe": tf,
            "timeframe_sec": tf_sec,
            "stale_after_sec": 2 * tf_sec,
            "stale": stale,
        },
        "as_of": _iso(clock),
        "state_path": str(state_path),
        "heartbeat_path": str(hb_path) if hb_path.is_file() else None,
        "heartbeat_loaded": bool(heartbeat_doc),
        "heartbeat_error": heartbeat_error,
        "read_only": True,
    }


def _connect_ro(path: Path) -> sqlite3.Connection:
    resolved = path.expanduser().resolve()
    uri = "file:" + quote(resolved.as_posix(), safe="/") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _meta(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM meta").fetchall()
    return {str(row["key"]): str(row["value"]) for row in rows}


def _positions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT symbol, qty, avg_price FROM positions").fetchall()
    return [
        {"symbol": str(row["symbol"]), "qty": float(row["qty"]), "avg_price": float(row["avg_price"])}
        for row in rows
    ]


def _latest_equity(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT ts, equity, cash FROM equity ORDER BY ts DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return {"ts": str(row["ts"]), "equity": float(row["equity"]), "cash": float(row["cash"])}


def _equity_values(conn: sqlite3.Connection) -> list[float]:
    rows = conn.execute("SELECT equity FROM equity ORDER BY ts ASC").fetchall()
    return [float(row["equity"]) for row in rows]


def _fills(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            """
            SELECT ts, symbol, side, qty_base, price, client_order_id, status, reason
            FROM fills
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )
    )


def _load_heartbeat(path: Path) -> tuple[dict[str, str], str | None]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return {}, "unreadable"
    except json.JSONDecodeError:
        return {}, "invalid json"
    if not isinstance(raw, dict):
        return {}, "invalid json"
    out: dict[str, str] = {}
    strategy = raw.get("strategy_id")
    if isinstance(strategy, str) and strategy.strip():
        out["strategy_id"] = strategy.strip()
    mode = raw.get("mode")
    if isinstance(mode, str) and mode.strip() in _MODES:
        out["mode"] = mode.strip()
    symbol = raw.get("symbol")
    if isinstance(symbol, str) and symbol.strip():
        out["symbol"] = symbol.strip()
    tf = raw.get("timeframe")
    if isinstance(tf, str) and tf.strip():
        try:
            timeframe_seconds(tf)
        except ValueError:
            pass
        else:
            out["timeframe"] = tf.strip()
    last = raw.get("last_bar_ts")
    if isinstance(last, str) and last.strip():
        normalized = _optional_ts(last)
        if normalized is not None:
            out["last_bar_ts"] = normalized
    return out, None


def _parse_risk(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _marks(
    positions: list[dict[str, Any]],
    equity: float | None,
    cash: float | None,
    fills: list[sqlite3.Row],
) -> dict[str, float]:
    open_positions = [row for row in positions if abs(row["qty"]) >= 1e-16]
    if len(open_positions) == 1 and equity is not None and cash is not None:
        qty = float(open_positions[0]["qty"])
        if abs(qty) >= 1e-16:
            mark = (equity - cash) / qty
            if mark > 0:
                return {str(open_positions[0]["symbol"]): mark}
    last_fill: dict[str, float] = {}
    for row in fills:
        if str(row["status"]) != "filled":
            continue
        symbol = str(row["symbol"])
        if symbol in last_fill:
            continue
        price = float(row["price"])
        if price > 0:
            last_fill[symbol] = price
    return {str(row["symbol"]): last_fill[symbol] for row in open_positions if str(row["symbol"]) in last_fill}


def _enrich_position(row: dict[str, Any], mark: float | None, equity: float | None) -> dict[str, Any]:
    qty = float(row["qty"])
    avg = float(row["avg_price"])
    pct = None
    if mark is not None and equity:
        pct = round(abs(qty * mark) / equity * 100.0, 4)
    return {
        "symbol": row["symbol"],
        "side": _side(qty),
        "qty": round(qty, 8),
        "avg_price": round(avg, 8),
        "mark_price": None if mark is None else round(mark, 8),
        "position_pct_equity": pct,
    }


def _unrealized(rows: list[dict[str, Any]]) -> float | None:
    if not rows:
        return 0.0
    total = 0.0
    for row in rows:
        if row["mark_price"] is None:
            return None
        total += (float(row["mark_price"]) - float(row["avg_price"])) * float(row["qty"])
    return total


def _primary_position(rows: list[dict[str, Any]], symbol: str | None) -> dict[str, Any]:
    if not rows:
        return {
            "symbol": symbol,
            "side": "flat",
            "qty": 0.0,
            "avg_price": None,
            "mark_price": None,
            "position_pct_equity": 0.0,
        }

    def _notional(row: dict[str, Any]) -> float:
        price = row["mark_price"] if row["mark_price"] is not None else row["avg_price"]
        return abs(float(row["qty"]) * float(price))

    return max(rows, key=_notional)


def _primary_symbol(rows: list[dict[str, Any]], fills: list[sqlite3.Row]) -> str | None:
    if rows:
        return str(_primary_position(rows, None)["symbol"])
    if fills:
        return str(fills[0]["symbol"])
    return None


def _strategy_from_fills(fills: list[sqlite3.Row]) -> str | None:
    for row in fills:
        strategy = strategy_from_client_order_id(str(row["client_order_id"]))
        if strategy:
            return strategy
    return None


def _fill_view(row: sqlite3.Row) -> dict[str, Any]:
    status = str(row["status"])
    reason = row["reason"]
    return {
        "ts": str(row["ts"]),
        "side": str(row["side"]),
        "qty": round(float(row["qty_base"]), 8),
        "price": round(float(row["price"]), 8),
        "client_order_id": str(row["client_order_id"]),
        "rejected_by_gate": status == "rejected",
        "status": status,
        "reason": None if reason is None or reason == "" else str(reason),
    }


def _resolve_timeframe(*candidates: str | None) -> str:
    for candidate in candidates:
        if candidate is None or not str(candidate).strip():
            continue
        try:
            timeframe_seconds(str(candidate))
        except ValueError:
            continue
        return str(candidate).strip()
    return "1h"


def _mode_from_meta(meta: dict[str, str]) -> str | None:
    mode = _clean(meta.get("mode"))
    if mode in _MODES:
        return mode
    return None


def _pct_change(current: float | None, base: float | None) -> float | None:
    if current is None or base is None or base == 0:
        return None
    return round((current / base - 1.0) * 100.0, 4)


def _float_or_none(raw: Any) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _round(value: float | None, ndigits: int = 8) -> float | None:
    if value is None:
        return None
    return round(float(value), ndigits)


def _side(qty: float) -> str:
    if qty > 1e-16:
        return "long"
    if qty < -1e-16:
        return "short"
    return "flat"


def _clean(raw: str | None) -> str | None:
    if raw is None:
        return None
    text = raw.strip()
    return text or None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_ts(raw: str) -> datetime:
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid timestamp {raw!r}") from exc
    return _as_utc(parsed)


def _optional_ts(raw: str | None) -> str | None:
    if raw is None or not str(raw).strip():
        return None
    try:
        return _iso(_parse_ts(str(raw)))
    except ValueError:
        return None


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat().replace("+00:00", "Z")
