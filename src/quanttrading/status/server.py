"""Loopback HTTP server for the read-only status page."""

from __future__ import annotations

import ipaddress
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from quanttrading.status.snapshot import StatusReadError, build_status

_HTML_PATH = Path(__file__).with_name("dashboard.html")
_CSP = "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'"


class StatusHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def assert_loopback(host: str) -> None:
    """Reject any bind address that is not IPv4 loopback or the name localhost."""
    if host == "localhost":
        return
    try:
        ip = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("status-ui only binds to loopback (127.0.0.1 or localhost)") from exc
    if ip.version != 4 or not ip.is_loopback:
        raise ValueError("status-ui only binds to loopback (127.0.0.1 or localhost)")


def make_server(
    *,
    state: Path,
    host: str,
    port: int,
    refresh_sec: int,
    heartbeat: Path | None,
    timeframe: str | None,
) -> StatusHTTPServer:
    assert_loopback(host)
    if not 5 <= int(refresh_sec) <= 15:
        raise ValueError("refresh_sec must be between 5 and 15")
    handler = _handler_class(
        state=state,
        refresh_sec=int(refresh_sec),
        heartbeat=heartbeat,
        timeframe=timeframe,
    )
    return StatusHTTPServer((host, port), handler)


def serve_status(
    *,
    state: Path,
    host: str,
    port: int,
    refresh_sec: int,
    heartbeat: Path | None,
    timeframe: str | None,
) -> None:
    """Serve GET / and GET /api/status until interrupted. No other methods are allowed."""
    httpd = make_server(
        state=state,
        host=host,
        port=port,
        refresh_sec=refresh_sec,
        heartbeat=heartbeat,
        timeframe=timeframe,
    )
    bound_host, bound_port = httpd.server_address[:2]
    print(
        f"status-ui  read-only  http://{bound_host}:{bound_port}/  state={state}  refresh={refresh_sec}s",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped", flush=True)
    finally:
        httpd.server_close()


def _render_index(refresh_sec: int) -> bytes:
    text = _HTML_PATH.read_text(encoding="utf-8")
    if "__REFRESH_SEC__" not in text:
        raise RuntimeError("dashboard template missing __REFRESH_SEC__")
    return text.replace("__REFRESH_SEC__", str(int(refresh_sec))).encode("utf-8")


def _handler_class(
    *,
    state: Path,
    refresh_sec: int,
    heartbeat: Path | None,
    timeframe: str | None,
) -> type[BaseHTTPRequestHandler]:
    index = _render_index(refresh_sec)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                _send(self, 200, "text/html; charset=utf-8", index)
                return
            if path == "/api/status":
                status, payload = _status_payload(state, heartbeat, timeframe)
                _send(self, status, "application/json; charset=utf-8", _dump(payload))
                return
            _send(self, 404, "application/json; charset=utf-8", _dump({"error": "not found", "read_only": True}))

        def do_POST(self) -> None:  # noqa: N802
            self._read_only()

        def do_PUT(self) -> None:  # noqa: N802
            self._read_only()

        def do_PATCH(self) -> None:  # noqa: N802
            self._read_only()

        def do_DELETE(self) -> None:  # noqa: N802
            self._read_only()

        def _read_only(self) -> None:
            body = _dump({"error": "read-only", "read_only": True})
            self.send_response(405)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Allow", "GET")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    return Handler


def _status_payload(
    state: Path,
    heartbeat: Path | None,
    timeframe: str | None,
) -> tuple[int, dict[str, Any]]:
    try:
        return 200, build_status(state, heartbeat=heartbeat, timeframe=timeframe)
    except FileNotFoundError as exc:
        return 404, {"error": str(exc), "read_only": True}
    except (StatusReadError, ValueError) as exc:
        return 500, {"error": str(exc), "read_only": True}


def _send(handler: BaseHTTPRequestHandler, status: int, content_type: str, body: bytes) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Content-Security-Policy", _CSP)
    handler.send_header("Connection", "close")
    handler.end_headers()
    handler.wfile.write(body)


def _dump(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")
