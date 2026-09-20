from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from quanttrading.execution.fills import Fill


class SQLiteStore:
    def __init__(self, path: Path | str | None) -> None:
        if path is None or str(path) == ":memory:":
            self._conn = sqlite3.connect(":memory:")
        else:
            db_path = Path(path)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        cur = self._conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS positions (
                symbol TEXT PRIMARY KEY,
                qty REAL NOT NULL,
                avg_price REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY,
                client_order_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                intent TEXT NOT NULL,
                qty_base REAL NOT NULL,
                price REAL NOT NULL,
                notional REAL NOT NULL,
                slippage_bps REAL NOT NULL,
                status TEXT NOT NULL,
                reason TEXT
            );
            CREATE TABLE IF NOT EXISTS equity (
                ts TEXT PRIMARY KEY,
                equity REAL NOT NULL,
                cash REAL NOT NULL
            );
            """
        )
        self._conn.commit()

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def load_risk_payload(self) -> dict[str, Any] | None:
        raw = self.get_meta("risk")
        if not raw:
            return None
        return json.loads(raw)

    def save_risk_payload(self, payload: dict[str, Any]) -> None:
        self.set_meta("risk", json.dumps(payload))

    def load_cash(self) -> float | None:
        raw = self.get_meta("cash")
        return None if raw is None else float(raw)

    def save_cash(self, cash: float) -> None:
        self.set_meta("cash", repr(cash))

    def load_positions(self) -> dict[str, tuple[float, float]]:
        rows = self._conn.execute("SELECT symbol, qty, avg_price FROM positions").fetchall()
        return {str(r["symbol"]): (float(r["qty"]), float(r["avg_price"])) for r in rows}

    def save_position(self, symbol: str, qty: float, avg_price: float) -> None:
        if abs(qty) < 1e-16:
            self._conn.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
            return
        self._conn.execute(
            """
            INSERT INTO positions(symbol, qty, avg_price) VALUES(?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET qty = excluded.qty, avg_price = excluded.avg_price
            """,
            (symbol, qty, avg_price),
        )

    def record_fill(self, fill: Fill, status: str = "filled", reason: str | None = None) -> None:
        self._conn.execute(
            """
            INSERT INTO fills(
                client_order_id, ts, symbol, side, intent, qty_base, price, notional, slippage_bps, status, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fill.client_order_id,
                fill.ts,
                fill.symbol,
                fill.side,
                fill.intent,
                fill.qty_base,
                fill.price,
                fill.notional,
                fill.slippage_bps,
                status,
                reason,
            ),
        )

    def record_reject(self, client_order_id: str, ts: str, symbol: str, reason: str) -> None:
        self._conn.execute(
            """
            INSERT INTO fills(
                client_order_id, ts, symbol, side, intent, qty_base, price, notional, slippage_bps, status, reason
            ) VALUES (?, ?, ?, 'none', 'open', 0, 0, 0, 0, 'rejected', ?)
            """,
            (client_order_id, ts, symbol, reason),
        )

    def record_equity(self, ts: str, equity: float, cash: float) -> None:
        self._conn.execute(
            """
            INSERT INTO equity(ts, equity, cash) VALUES(?, ?, ?)
            ON CONFLICT(ts) DO UPDATE SET equity = excluded.equity, cash = excluded.cash
            """,
            (ts, equity, cash),
        )

    def fills(self) -> list[sqlite3.Row]:
        return list(self._conn.execute("SELECT * FROM fills ORDER BY id ASC"))

    def equity_curve(self) -> list[tuple[str, float]]:
        rows = self._conn.execute("SELECT ts, equity FROM equity ORDER BY ts ASC").fetchall()
        return [(str(r["ts"]), float(r["equity"])) for r in rows]

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()
