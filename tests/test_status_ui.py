from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from pathlib import Path

import pytest
from typer.testing import CliRunner

from quanttrading.cli import app
from quanttrading.config import Settings
from quanttrading.execution.paper import PaperBroker
from quanttrading.status.server import assert_loopback, make_server
from quanttrading.status.snapshot import (
    build_status,
    strategy_from_client_order_id,
    timeframe_seconds,
)
from tests.helpers import make_bar, make_signal

UTC = timezone.utc
TS = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)
RUNNER = CliRunner()


def _settings(**overrides: object) -> Settings:
    payload = dict(
        paper_equity=2000,
        per_trade_pct=0.01,
        daily_dd_pct=0.03,
        total_dd_pct=0.20,
        max_slippage_bps=0,
    )
    payload.update(overrides)
    return Settings(**payload)


def _book(path: Path, *, close: float = 110.0) -> Path:
    broker = PaperBroker(_settings(), store_path=path)
    opened = make_bar(close=100.0, ts=TS)
    broker.mark_to_market(opened)
    report = broker.submit(
        make_signal(
            strategy_id="sma_cross_v2",
            client_order_id="sma_cross_v2-BTCUSD-20240601T120000Z-open-buy",
            qty=20.0,
            qty_unit="quote",
            max_slippage_bps=0,
        ),
        opened,
    )
    assert report.status == "filled"
    broker.mark_to_market(make_bar(close=close, ts=TS + timedelta(hours=1)))
    broker.store.close()
    return path


def _keys(obj: object):
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield key
            yield from _keys(value)
    elif isinstance(obj, list):
        for item in obj:
            yield from _keys(item)


def test_strategy_id_parser() -> None:
    assert strategy_from_client_order_id("sma_cross_v2-BTCUSD-20240601T120000Z-open-buy") == "sma_cross_v2"
    assert strategy_from_client_order_id("my-strategy-v1-ETHUSD-20240601T120000Z-close-sell") == "my-strategy-v1"
    assert strategy_from_client_order_id("deadbeef") is None


def test_timeframe_seconds() -> None:
    assert timeframe_seconds("15m") == 900
    assert timeframe_seconds("1h") == 3600
    assert timeframe_seconds("1d") == 86400
    with pytest.raises(ValueError):
        timeframe_seconds("2w")


def test_snapshot_overview_position_and_fills(tmp_path: Path) -> None:
    path = _book(tmp_path / "paper.sqlite")
    snap = build_status(path, now=datetime(2024, 6, 1, 14, 0, tzinfo=UTC), timeframe="1h")
    overview = snap["overview"]
    assert overview["strategy_id"] == "sma_cross_v2"
    assert overview["mode"] == "paper"
    assert overview["symbol"] == "BTC/USD"
    assert overview["cash"] == pytest.approx(1980.0)
    assert overview["equity"] == pytest.approx(2002.0)
    assert overview["unrealized_pnl"] == pytest.approx(2.0)
    assert overview["day_pnl_pct"] == pytest.approx(0.1)
    assert overview["rolling_pnl_pct"] == pytest.approx(0.1)
    assert overview["max_dd_pct"] == pytest.approx(0.0)
    assert overview["halted"] is False
    assert overview["halt_reason"] is None
    position = snap["position"]
    assert position["side"] == "long"
    assert position["qty"] == pytest.approx(0.2)
    assert position["avg_price"] == pytest.approx(100.0)
    assert position["mark_price"] == pytest.approx(110.0)
    assert position["position_pct_equity"] == pytest.approx(22 / 2002 * 100, abs=1e-4)
    assert snap["fills"][0]["rejected_by_gate"] is False
    assert snap["fills"][0]["client_order_id"].startswith("sma_cross_v2-")
    assert snap["heartbeat"]["last_bar_ts"] == "2024-06-01T13:00:00Z"
    assert snap["heartbeat"]["stale"] is False
    assert snap["read_only"] is True


def test_short_unrealized_pnl(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite"
    broker = PaperBroker(_settings(), store_path=path)
    bar = make_bar(close=100.0, ts=TS)
    broker.submit(
        make_signal(
            side="sell",
            intent="open",
            qty=0.2,
            qty_unit="base",
            max_slippage_bps=0,
            client_order_id="sma_cross_v2-BTCUSD-20240601T120000Z-open-sell",
        ),
        bar,
    )
    broker.mark_to_market(make_bar(close=90.0, ts=TS + timedelta(hours=1)))
    broker.store.close()
    snap = build_status(path, now=TS + timedelta(hours=2))
    assert snap["position"]["side"] == "short"
    assert snap["position"]["qty"] == pytest.approx(-0.2)
    assert snap["position"]["mark_price"] == pytest.approx(90.0)
    assert snap["overview"]["unrealized_pnl"] == pytest.approx(2.0)


def test_rejected_fill_is_flagged(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite"
    broker = PaperBroker(_settings(), store_path=path)
    bar = make_bar(close=100.0, ts=TS)
    report = broker.submit(
        make_signal(
            qty=50.0,
            qty_unit="quote",
            client_order_id="sma_cross_v2-BTCUSD-20240601T120000Z-open-buy",
        ),
        bar,
    )
    assert report.status == "rejected"
    broker.store.close()
    snap = build_status(path, now=TS)
    fill = snap["fills"][0]
    assert fill["rejected_by_gate"] is True
    assert fill["reason"] == "per_trade_limit"
    assert snap["position"]["side"] == "flat"
    assert snap["overview"]["strategy_id"] == "sma_cross_v2"


def test_halt_uses_persisted_peak_when_equity_timestamp_collapses(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite"
    broker = PaperBroker(_settings(), store_path=path)
    bar = make_bar(close=100.0, ts=TS)
    broker.mark_to_market(bar)
    broker.cash = 1600.0
    broker.mark_to_market(bar)
    assert broker.risk.halted
    broker.store.close()
    snap = build_status(path, now=TS + timedelta(hours=3), timeframe="1h")
    assert snap["overview"]["halted"] is True
    assert snap["overview"]["halt_reason"] == "max_drawdown"
    assert snap["overview"]["max_dd_pct"] == pytest.approx(20.0)
    assert snap["overview"]["day_pnl_pct"] == pytest.approx(-20.0)
    assert snap["heartbeat"]["stale"] is True


def test_daily_breaker_is_separate_from_sticky_halt(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite"
    broker = PaperBroker(_settings(), store_path=path)
    bar = make_bar(close=100.0, ts=TS)
    broker.mark_to_market(bar)
    broker.cash = 2000.0 - 60.0
    broker.mark_to_market(bar)
    broker.store.close()
    snap = build_status(path, now=TS)
    assert snap["overview"]["halted"] is False
    assert snap["overview"]["halt_reason"] is None
    assert snap["overview"]["daily_breaker_active"] is True


def test_stale_boundary(tmp_path: Path) -> None:
    path = _book(tmp_path / "paper.sqlite")
    last = datetime(2024, 6, 1, 13, 0, tzinfo=UTC)
    fresh = build_status(path, now=last + timedelta(seconds=7200), timeframe="1h")
    stale = build_status(path, now=last + timedelta(seconds=7201), timeframe="1h")
    assert fresh["heartbeat"]["stale"] is False
    assert stale["heartbeat"]["stale"] is True
    assert stale["heartbeat"]["lag_sec"] == pytest.approx(7201.0)


def test_heartbeat_overrides_and_drops_secrets(tmp_path: Path) -> None:
    path = _book(tmp_path / "paper.sqlite")
    heartbeat = tmp_path / "paper.heartbeat.json"
    heartbeat.write_text(
        json.dumps(
            {
                "strategy_id": "mean_reversion_v1",
                "mode": "dry-run",
                "symbol": "ETH/USD",
                "timeframe": "15m",
                "last_bar_ts": "2026-09-22T01:40:00Z",
                "api_key": "SUPERSECRETKEY",
                "api_secret": "SUPERSECRETVALUE",
                "token": "Bearer abc",
            }
        ),
        encoding="utf-8",
    )
    snap = build_status(path, now=datetime(2026, 9, 22, 1, 45, tzinfo=UTC))
    assert snap["overview"]["strategy_id"] == "mean_reversion_v1"
    assert snap["overview"]["mode"] == "dry-run"
    assert snap["overview"]["symbol"] == "BTC/USD"
    assert snap["heartbeat"]["timeframe"] == "15m"
    assert snap["heartbeat"]["last_bar_ts"] == "2026-09-22T01:40:00Z"
    assert snap["heartbeat"]["stale"] is False
    assert snap["heartbeat_loaded"] is True
    blob = json.dumps(snap)
    assert "SUPERSECRET" not in blob
    assert "Bearer" not in blob
    assert "api_key" not in set(_keys(snap))
    assert "api_secret" not in set(_keys(snap))
    assert "token" not in set(_keys(snap))


def test_explicit_heartbeat_path(tmp_path: Path) -> None:
    path = _book(tmp_path / "book.sqlite")
    heartbeat = tmp_path / "live.json"
    heartbeat.write_text(
        json.dumps({"mode": "dry-run", "strategy_id": "sma_cross_v2", "timeframe": "5m", "last_bar_ts": "2024-06-01T13:00:00Z"}),
        encoding="utf-8",
    )
    snap = build_status(path, heartbeat=heartbeat, now=datetime(2024, 6, 1, 13, 4, tzinfo=UTC))
    assert snap["overview"]["mode"] == "dry-run"
    assert snap["heartbeat"]["timeframe"] == "5m"
    assert snap["heartbeat"]["stale"] is False


def test_invalid_heartbeat_is_ignored(tmp_path: Path) -> None:
    path = _book(tmp_path / "paper.sqlite")
    (tmp_path / "paper.heartbeat.json").write_text("{not json", encoding="utf-8")
    snap = build_status(path, now=datetime(2024, 6, 1, 14, 0, tzinfo=UTC))
    assert snap["heartbeat_error"] == "invalid json"
    assert snap["overview"]["strategy_id"] == "sma_cross_v2"
    assert snap["overview"]["mode"] == "paper"


def test_snapshot_does_not_create_or_modify_state(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "paper.sqlite"
    with pytest.raises(FileNotFoundError):
        build_status(missing)
    assert not missing.exists()
    assert not missing.parent.exists()

    path = _book(tmp_path / "paper.sqlite")
    before = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    build_status(path, now=datetime(2024, 6, 1, 14, 0, tzinfo=UTC))
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime


def test_empty_book(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite"
    broker = PaperBroker(_settings(), store_path=path)
    broker.store.close()
    snap = build_status(path, now=TS)
    assert snap["overview"]["equity"] == pytest.approx(2000.0)
    assert snap["overview"]["cash"] == pytest.approx(2000.0)
    assert snap["overview"]["unrealized_pnl"] == pytest.approx(0.0)
    assert snap["overview"]["strategy_id"] is None
    assert snap["position"]["side"] == "flat"
    assert snap["fills"] == []
    assert snap["heartbeat"]["stale"] is True
    assert snap["heartbeat"]["last_bar_ts"] is None


def test_loopback_guard() -> None:
    assert_loopback("127.0.0.1")
    assert_loopback("localhost")
    with pytest.raises(ValueError):
        assert_loopback("0.0.0.0")
    with pytest.raises(ValueError):
        assert_loopback("192.168.1.10")


def test_http_is_read_only(tmp_path: Path) -> None:
    path = _book(tmp_path / "paper.sqlite")
    html_path = Path(__file__).resolve().parents[1] / "src" / "quanttrading" / "status" / "dashboard.html"
    template = html_path.read_text(encoding="utf-8")
    assert "<form" not in template.lower()
    for label in (
        "Strategy",
        "Mode",
        "Symbol",
        "Equity",
        "Cash",
        "Unrealized PnL",
        "Day PnL %",
        "Rolling PnL %",
        "Max DD %",
        "Halted",
        "Halt reason",
        "Avg price",
        "Mark price",
        "% of equity",
        "Client order id",
        "Lag vs now",
    ):
        assert label in template
    lowered = template.lower()
    assert "<button" not in lowered
    assert "method=\"post\"" not in lowered
    assert "api/orders" not in lowered
    assert "__REFRESH_SEC__" in template

    httpd = make_server(state=path, host="127.0.0.1", port=0, refresh_sec=10, heartbeat=None, timeframe="1h")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        page = _request(port, "GET", "/")
        assert page.status == 200
        assert b"Refreshes every 10s" in page.body
        assert b"<form" not in page.body.lower()
        status = _request(port, "GET", "/api/status")
        assert status.status == 200
        payload = json.loads(status.body)
        assert payload["overview"]["mode"] == "paper"
        assert payload["overview"]["symbol"] == "BTC/USD"
        assert payload["read_only"] is True
        posted = _request(port, "POST", "/api/status", b"{}")
        assert posted.status == 405
        assert json.loads(posted.body)["read_only"] is True
        deleted = _request(port, "DELETE", "/")
        assert deleted.status == 405
        missing = _request(port, "GET", "/api/orders")
        assert missing.status == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    gone = tmp_path / "absent.sqlite"
    httpd = make_server(state=gone, host="127.0.0.1", port=0, refresh_sec=5, heartbeat=None, timeframe=None)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        missing_state = _request(port, "GET", "/api/status")
        assert missing_state.status == 404
        assert "not found" in json.loads(missing_state.body)["error"]
        assert not gone.exists()
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_refresh_outside_server_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        make_server(state=tmp_path / "x.sqlite", host="127.0.0.1", port=0, refresh_sec=1, heartbeat=None, timeframe=None)


def test_status_ui_cli_guardrails(tmp_path: Path) -> None:
    help_result = RUNNER.invoke(app, ["status-ui", "--help"])
    assert help_result.exit_code == 0
    assert "--state" in help_result.output
    assert "--refresh-sec" in help_result.output
    assert "127.0.0.1" in help_result.output
    assert "read-only" in help_result.output.lower()

    public = RUNNER.invoke(app, ["status-ui", "--host", "0.0.0.0", "--state", str(tmp_path / "paper.sqlite")])
    assert public.exit_code != 0
    assert "loopback" in public.output.lower()

    fast = RUNNER.invoke(app, ["status-ui", "--refresh-sec", "1"])
    assert fast.exit_code != 0

    bad_tf = RUNNER.invoke(app, ["status-ui", "--timeframe", "2w"])
    assert bad_tf.exit_code != 0

    missing_hb = RUNNER.invoke(app, ["status-ui", "--heartbeat", str(tmp_path / "nope.json")])
    assert missing_hb.exit_code != 0
    assert "heartbeat file not found" in missing_hb.output


class _Response:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self.body = body


def _request(port: int, method: str, path: str, body: bytes | None = None) -> _Response:
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=body)
        res = conn.getresponse()
        return _Response(res.status, res.read())
    finally:
        conn.close()
