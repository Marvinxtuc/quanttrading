"""Rolling OOS report helpers and CLI smoke."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from typer.testing import CliRunner

from quanttrading.cli import app
from quanttrading.data.ohlcv import save_csv
from quanttrading.market import Bar
from quanttrading.oos.report import (
    build_expanding_folds,
    buy_and_hold_1pct,
    pair_round_trips,
    render_oos_markdown,
    run_oos_report,
)


UTC = timezone.utc


def _bars(n: int, *, start: datetime | None = None, step_hours: int = 4) -> list[Bar]:
    ts = start or datetime(2020, 1, 1, tzinfo=UTC)
    price = 10_000.0
    out: list[Bar] = []
    for i in range(n):
        o = price
        c = price * (1.0 + (0.001 if (i // 40) % 2 == 0 else -0.0008))
        out.append(
            Bar(
                ts=ts,
                symbol="BTC/USD",
                open=o,
                high=max(o, c) * 1.001,
                low=min(o, c) * 0.999,
                close=c,
                volume=1.0,
            )
        )
        price = c
        ts += timedelta(hours=step_hours)
    return out


def test_build_expanding_folds_includes_holdout() -> None:
    folds = build_expanding_folds(1000, holdout_frac=0.2, test_frac=0.15, min_train_frac=0.35)
    assert folds[-1].kind == "holdout"
    assert folds[-1].fold_id == "holdout"
    assert folds[-1].test_end == 1000
    assert all(f.test_end <= folds[-1].test_start or f is folds[-1] for f in folds)
    assert len([f for f in folds if f.kind == "walk_forward"]) >= 1


def test_pair_round_trips_and_buy_hold() -> None:
    class Row(dict):
        pass

    fills = [
        Row(status="filled", intent="open", side="buy", ts="2024-01-01T00:00:00Z", price=100.0, qty_base=0.1, notional=10.0),
        Row(status="filled", intent="close", side="sell", ts="2024-01-02T00:00:00Z", price=110.0, qty_base=0.1, notional=11.0),
    ]
    trips = pair_round_trips(fills)
    assert len(trips) == 1
    assert abs(trips[0].pnl - 1.0) < 1e-9
    bars = _bars(10)
    pnl, ret = buy_and_hold_1pct(bars, test_start=0, test_end=10, equity=2000.0, per_trade_pct=0.01)
    assert isinstance(pnl, float)
    assert isinstance(ret, float)


def test_oos_report_runs_on_synthetic_and_cli(tmp_path: Path) -> None:
    bars = _bars(400)
    report = run_oos_report(
        bars,
        holdout_frac=0.2,
        test_frac=0.2,
        min_train_frac=0.4,
    )
    assert report["span"]["n_bars"] == 400
    assert "trend_breakout_v1" in report["strategies"]
    assert "range_reversion_v2" in report["strategies"]
    assert "0.003" in report["strategies"]["trend_breakout_v1"]["by_cost"]
    assert "0.006" in report["strategies"]["trend_breakout_v1"]["by_cost"]
    md = render_oos_markdown(report)
    assert "After-cost EV" in md
    assert "fee estimate" in md.lower() or "Fee" in md

    csv_path = tmp_path / "long.csv"
    save_csv(bars, csv_path)
    out = tmp_path / "oos.md"
    json_out = tmp_path / "oos.json"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "oos-report",
            "--data",
            str(csv_path),
            "--out",
            str(out),
            "--json-out",
            str(json_out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert json_out.exists()
    assert "oos-report:" in result.output


def test_fetch_history_from_local_source(tmp_path: Path) -> None:
    raw = tmp_path / "XBTUSD_240.csv"
    base = int(datetime(2021, 1, 1, tzinfo=UTC).timestamp())
    lines = []
    px = 30_000.0
    for i in range(30):
        lines.append(f"{base + i * 14400},{px},{px + 1},{px - 1},{px + 0.5},1.0,1")
        px += 10
    raw.write_text("\n".join(lines) + "\n", encoding="utf-8")
    out = tmp_path / "out.csv"
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["fetch-history", "--source", str(raw), "--out", str(out), "--work-dir", str(tmp_path / "work")],
    )
    assert result.exit_code == 0, result.output
    assert out.exists()
    assert "wrote 30 4h bars" in result.output
