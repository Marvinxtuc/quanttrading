"""ma_ensemble_v1: paper-only 3-sleeve daily SMA ensemble (see docs/ma_ensemble_v1.md).

Sleeves A/B/C = SMA150/200/250 on Kraken BTC/USD daily bars (UTC 00:00 close).
Long-only, no leverage. A sleeve trades only when its target position changes.
Never sends real orders: ``orders_sent`` is always 0.
"""

from __future__ import annotations

import json
import sqlite3
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Sequence

from .live_prereqs import CostGuard, realized_cost_bps

STRATEGY_ID = "ma_ensemble_v1"
SLEEVES = (("A", 150), ("B", 200), ("C", 250))
START_CAPITAL = 200.0
COST_PER_SIDE = 0.0040
SLIPPAGE = 0.0005
MARKET_EXTRA_SLIPPAGE = 0.0005  # extra slippage assumption for the market fallback
MIN_ORDER_BTC = 0.00005
MAX_PORTFOLIO_DD = 0.60
KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=1440"
MIN_ORDER_ETH = 0.002  # Kraken public AssetPairs XETHZUSD ordermin (checked 2026-11-02)

# pair -> (asset label, OHLC url, min order qty, state file stem)
PAIRS = {
    "XBTUSD": ("BTC", KRAKEN_OHLC_URL, MIN_ORDER_BTC, "paper_sma_basket"),
    "ETHUSD": ("ETH", "https://api.kraken.com/0/public/OHLC?pair=XETHZUSD&interval=1440", MIN_ORDER_ETH, "paper_eth_basket"),
}
PAIR_ALIASES = {"BTCUSD": "XBTUSD", "XXBTZUSD": "XBTUSD", "XETHZUSD": "ETHUSD"}


def resolve_pair(pair: str) -> str:
    p = pair.upper().replace("/", "")
    p = PAIR_ALIASES.get(p, p)
    if p not in PAIRS:
        raise ValueError(f"unsupported pair {pair!r}; choose from {sorted(PAIRS)}")
    return p


@dataclass(frozen=True)
class DailyBar:
    day: str  # UTC date the bar opens (YYYY-MM-DD)
    open: float
    high: float
    low: float
    close: float


def fetch_kraken_daily(url: str = KRAKEN_OHLC_URL, timeout: float = 30.0) -> list[DailyBar]:
    """Public Kraken OHLC (no key). Returns up to ~720 bars incl. the in-progress one last."""
    req = urllib.request.Request(url, headers={"User-Agent": "quanttrading-paper"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.load(resp)
    if payload.get("error"):
        raise RuntimeError(f"Kraken OHLC error: {payload['error']}")
    rows = next(v for k, v in payload["result"].items() if k != "last")
    return [
        DailyBar(
            day=datetime.fromtimestamp(int(r[0]), tz=timezone.utc).date().isoformat(),
            open=float(r[1]), high=float(r[2]), low=float(r[3]), close=float(r[4]),
        )
        for r in rows
    ]


def sma(closes: Sequence[float], n: int) -> float | None:
    return None if len(closes) < n else sum(closes[-n:]) / n


def target_position(close: float, sma_value: float | None) -> int:
    """1 = 100% BTC if close > SMA, else 0 (cash). Unknown SMA → cash."""
    return 1 if sma_value is not None and close > sma_value else 0


def simulate_fill(side: str, limit_px: float, session: DailyBar | None) -> tuple[float, str]:
    """Limit first at ``limit_px``; if the session range never reached it, market fallback.

    With the bar for the execution day: a buy limit fills if low <= limit, a sell if
    high >= limit. Placed at the open/best price, it is touched by construction, so
    the fallback only triggers on degenerate data (``session`` missing a touch).
    """
    if session is None or (side == "buy" and session.low <= limit_px) or (side == "sell" and session.high >= limit_px):
        return limit_px, "limit"
    ref = session.close
    slip = SLIPPAGE + MARKET_EXTRA_SLIPPAGE
    return (ref * (1 + slip) if side == "buy" else ref * (1 - slip)), "market_fallback"


SCHEMA = """
CREATE TABLE IF NOT EXISTS sleeves (name TEXT PRIMARY KEY, sma_n INTEGER, cash REAL, btc REAL);
CREATE TABLE IF NOT EXISTS runs (signal_day TEXT PRIMARY KEY, run_ts TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS fills (id INTEGER PRIMARY KEY AUTOINCREMENT, signal_day TEXT, sleeve TEXT,
  side TEXT, qty_btc REAL, price REAL, fee REAL, fill_type TEXT, reason TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


class Ledger:
    def __init__(self, path: Path | str):
        self.db = sqlite3.connect(str(path))
        self.db.executescript(SCHEMA)
        if not self.db.execute("SELECT 1 FROM sleeves").fetchone():
            for name, n in SLEEVES:
                self.db.execute("INSERT INTO sleeves VALUES (?,?,?,0)", (name, n, START_CAPITAL / len(SLEEVES)))
            self.db.execute("INSERT INTO meta VALUES ('peak_equity', ?)", (str(START_CAPITAL),))
            self.db.execute("INSERT INTO meta VALUES ('halted', '0')")
            self.db.commit()

    def sleeves(self) -> dict[str, dict]:
        # DB column is still named `btc` (legacy); it holds the base-asset quantity for any pair.
        cols = [c[1] for c in self.db.execute("PRAGMA table_info(sleeves)")]
        qcol = "qty" if "qty" in cols else "btc"
        return {r[0]: {"sma_n": r[1], "cash": r[2], "qty": r[3]}
                for r in self.db.execute(f"SELECT name, sma_n, cash, {qcol} FROM sleeves")}

    def meta(self, k: str) -> str:
        return self.db.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()[0]

    def set_meta(self, k: str, v: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (k, v))

    def cost_guard(self) -> CostGuard:
        r = self.db.execute("SELECT v FROM meta WHERE k='cost_guard'").fetchone()
        return CostGuard.from_dict(json.loads(r[0])) if r else CostGuard()

    def save_cost_guard(self, g: CostGuard) -> None:
        self.set_meta("cost_guard", json.dumps(g.to_dict()))

    def done(self, day: str) -> dict | None:
        r = self.db.execute("SELECT summary FROM runs WHERE signal_day=?", (day,)).fetchone()
        return json.loads(r[0]) if r else None

    def replay_positions(self) -> dict[str, tuple[float, float]]:
        """Recompute cash/btc per sleeve from the fill ledger (for reconciliation)."""
        pos = {name: [START_CAPITAL / len(SLEEVES), 0.0] for name, _ in SLEEVES}
        for sleeve, side, qty, px, fee in self.db.execute("SELECT sleeve, side, qty_btc, price, fee FROM fills ORDER BY id"):
            if side == "buy":
                pos[sleeve][0] -= qty * px + fee
                pos[sleeve][1] += qty
            else:
                pos[sleeve][0] += qty * px - fee
                pos[sleeve][1] -= qty
        return {k: (v[0], v[1]) for k, v in pos.items()}


def run_step(
    bars: Sequence[DailyBar],
    ledger: Ledger,
    *,
    now: datetime | None = None,
    log_path: Path | None = None,
    min_order: float = MIN_ORDER_BTC,
    asset: str = "BTC",
) -> dict:
    """One idempotent daily step keyed on the latest completed UTC daily bar."""
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    completed = [b for b in bars if b.day < today]
    if len(completed) < max(n for _, n in SLEEVES):
        raise ValueError(f"need >= 250 completed daily bars, got {len(completed)}")
    signal_bar = completed[-1]
    exec_bar = next((b for b in bars if b.day == today), None)
    prior = ledger.done(signal_bar.day)
    if prior is not None:
        prior = normalize_summary(prior)
        prior = dict(prior, already_processed=True, trades_recorded_earlier=prior.get("trades_today", []),
                     trades_today=[], run_ts=now.isoformat(), recorded_run_ts=prior.get("run_ts"))
        return prior

    closes = [b.close for b in completed]
    fill_px = exec_bar.open if exec_bar else signal_bar.close  # next daily open (≈ price just after 00:00 UTC)
    sleeves = ledger.sleeves()
    halted = ledger.meta("halted") == "1"
    guard = ledger.cost_guard()
    cost_paused = guard.paused
    trades: list[dict] = []
    notes: list[str] = []
    ts = now.isoformat()

    def trade(name: str, side: str, qty: float, reason: str) -> None:
        s = sleeves[name]
        if qty < min_order:
            notes.append(f"{name}: {side} {qty:.8f} {asset} below Kraken min {min_order}; skipped")
            return
        limit_px = fill_px * (1 + SLIPPAGE) if side == "buy" else fill_px * (1 - SLIPPAGE)
        px, ftype = simulate_fill(side, limit_px, exec_bar)
        fee = qty * px * COST_PER_SIDE
        if side == "buy":
            s["cash"] -= qty * px + fee
            s["qty"] += qty
        else:
            s["cash"] += qty * px - fee
            s["qty"] -= qty
        ledger.db.execute(
            "INSERT INTO fills (signal_day, sleeve, side, qty_btc, price, fee, fill_type, reason, ts) VALUES (?,?,?,?,?,?,?,?,?)",
            (signal_bar.day, name, side, qty, px, fee, ftype, reason, ts),
        )
        cost = realized_cost_bps(side, qty, px, fee, fill_px)
        if guard.record(cost):
            notes.append(f"COST GUARD: {guard.consecutive} consecutive fills > {guard.limit_bps:.0f} bps; strategy paused, manual review")
        trades.append({"cost_bps": round(cost, 2), "sleeve": name, "side": side, "asset": asset, "qty": round(qty, 8), **({"qty_btc": round(qty, 8)} if asset == "BTC" else {}), "price": round(px, 2),
                       "fee": round(fee, 4), "fill_type": ftype, "reason": reason})

    # Pre-trade drawdown check at the execution price.
    equity_pre = sum(s["cash"] + s["qty"] * fill_px for s in sleeves.values())
    peak = max(float(ledger.meta("peak_equity")), equity_pre)
    if not halted and equity_pre < peak * (1 - MAX_PORTFOLIO_DD):
        halted = True
        notes.append(f"HARD STOP: portfolio DD {1 - equity_pre / peak:.1%} > {MAX_PORTFOLIO_DD:.0%}; all to cash, manual review")
        ledger.set_meta("halted", "1")

    if cost_paused and not halted:
        notes.append("cost guard paused: no new orders until manual review (positions held)")

    sleeve_rows = {}
    first_run = ledger.db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    for name, n in SLEEVES:
        s = sleeves[name]
        sv = sma(closes, n)
        tgt = 0 if halted else target_position(signal_bar.close, sv)
        cur = 1 if s["qty"] * fill_px > s["cash"] else 0
        if cost_paused and not halted:
            tgt = cur  # paused: hold, do not trade
        if tgt != cur or (tgt == 0 and s["qty"] > 0 and halted):
            reason = "halt" if halted else ("initial_entry" if first_run else "signal_change")
            if tgt == 1:
                trade(name, "buy", s["cash"] / (fill_px * (1 + SLIPPAGE) * (1 + COST_PER_SIDE)), reason)
            else:
                trade(name, "sell", s["qty"], reason)
        sleeve_rows[name] = {"sma_n": n, "sma": round(sv, 2) if sv else None, "signal": "LONG" if tgt else "CASH",
                             "target": tgt, "asset": asset, "qty": round(s["qty"], 8),
                             **({"btc": round(s["qty"], 8)} if asset == "BTC" else {}), "cash": round(s["cash"], 4),
                             "equity": round(s["cash"] + s["qty"] * fill_px, 4)}
        ledger.db.execute("UPDATE sleeves SET cash=?, btc=? WHERE name=?", (s["cash"], s["qty"], name))

    equity = sum(r["equity"] for r in sleeve_rows.values())
    peak = max(peak, equity)
    ledger.set_meta("peak_equity", str(peak))
    ledger.save_cost_guard(guard)

    # Reconciliation: stored state vs replay of fills, and position vs target.
    replay = ledger.replay_positions()
    mismatches = []
    for name, (c, b) in replay.items():
        s = sleeves[name]
        if abs(c - s["cash"]) > 1e-6 or abs(b - s["qty"]) > 1e-10:
            mismatches.append(f"{name}: ledger replay differs from state")
        held = 1 if s["qty"] * fill_px > s["cash"] else 0
        if held != sleeve_rows[name]["target"] and not any(m.startswith(name) for m in notes):
            mismatches.append(f"{name}: position {held} != target {sleeve_rows[name]['target']}")

    summary = {
        "strategy_id": STRATEGY_ID, "mode": "paper", "asset": asset, "min_order": min_order, "run_ts": ts, "signal_day": signal_bar.day,
        "signal_close": signal_bar.close, "exec_day": today, "exec_price": fill_px,
        "sleeves": sleeve_rows, "total_equity": round(equity, 4), "peak_equity": round(peak, 4),
        "drawdown": round(1 - equity / peak, 4), "halted": halted,
        "cost_guard": {"paused": guard.paused, "consecutive_breaches": guard.consecutive, "limit_bps": guard.limit_bps}, "trades_today": trades,
        "notes": notes, "reconciliation": {"mismatches": len(mismatches), "details": mismatches},
        "orders_sent": 0, "already_processed": False,
    }
    ledger.db.execute("INSERT INTO runs VALUES (?,?,?)", (signal_bar.day, ts, json.dumps(summary)))
    ledger.db.commit()
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(summary) + "\n")
    return summary


def normalize_summary(s: dict) -> dict:
    """Add generic `qty`/`asset` to summaries written by older versions (which only had `btc`/`qty_btc`)."""
    asset = s.get("asset", "BTC")
    s = dict(s, asset=asset)
    s["sleeves"] = {k: dict(r, asset=r.get("asset", asset), qty=r.get("qty", r.get("btc", 0.0)))
                    for k, r in s.get("sleeves", {}).items()}
    s["trades_today"] = [dict(t, asset=t.get("asset", asset), qty=t.get("qty", t.get("qty_btc")))
                         for t in s.get("trades_today", [])]
    return s


def _fmt_trades(trades: list[dict]) -> str:
    return "; ".join(f"{t['sleeve']} {t['side']} {t['qty']} {t.get('asset', '')} @ {t['price']} ({t['fill_type']})".replace("  ", " ")
                     for t in trades)


def format_summary(s: dict) -> str:
    s = normalize_summary(s)
    if s.get("already_processed"):
        s["trades_recorded_earlier"] = [dict(t, qty=t.get("qty", t.get("qty_btc"))) for t in s.get("trades_recorded_earlier", [])]
    rerun = s.get("already_processed")
    lines = [f"[{s['strategy_id']} paper] signal day {s['signal_day']} close={s['signal_close']:.2f}  "
             f"exec {s['exec_day']} @ {s['exec_price']:.2f}"
             + (f"  (ALREADY PROCESSED in earlier run at {s.get('recorded_run_ts')}; no new trades this run)" if rerun else "")]
    for name, r in s["sleeves"].items():
        lines.append(f"  {name} SMA{r['sma_n']}={r['sma']}  signal={r['signal']}  qty={r['qty']:.8f} {r['asset']}  cash={r['cash']:.2f}  equity={r['equity']:.2f}")
    lines.append(f"  total equity={s['total_equity']:.2f}  peak={s['peak_equity']:.2f}  dd={s['drawdown']:.2%}  halted={s['halted']}")
    if rerun:
        rec = s.get("trades_recorded_earlier", [])
        lines.append(f"  trades this run=0; trades recorded earlier for this day={len(rec)} (already executed)  " + _fmt_trades(rec))
    else:
        lines.append(f"  trades today={len(s['trades_today'])}  " + _fmt_trades(s["trades_today"]))
    for n in s["notes"]:
        lines.append(f"  note: {n}")
    lines.append(f"  reconciliation mismatches={s['reconciliation']['mismatches']}  orders_sent={s['orders_sent']}")
    return "\n".join(lines)


def run_once(state_dir: Path, fetch: Callable[[], list[DailyBar]] | None = None, now: datetime | None = None,
             pair: str = "XBTUSD") -> dict:
    asset, url, min_order, stem = PAIRS[resolve_pair(pair)]
    if fetch is None:
        fetch = lambda: fetch_kraken_daily(url)  # noqa: E731
    state_dir.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(state_dir / f"{stem}.sqlite")
    return run_step(fetch(), ledger, now=now, log_path=state_dir / f"{stem}.log", min_order=min_order, asset=asset)
