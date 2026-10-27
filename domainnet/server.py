"""Zero-dependency web dashboard server (standard library only).

Endpoints
---------
GET  /                 the live dashboard
GET  /api/state        full snapshot (topology, services, templates, stats)
GET  /api/events       recent events (?since=SEQ&limit=N) - for polling clients
GET  /api/stream       Server-Sent Events: a snapshot, then every event live
POST /api/command      JSON ``{"cmd": ...}`` or ``{"line": "link a b"}``
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from .control import Controller
from .core import Network

log = logging.getLogger("domainnet.server")
_STATIC = Path(__file__).parent / "static"


class _Handler(BaseHTTPRequestHandler):
    server_version = "domainnet"

    def log_message(self, fmt: str, *args: Any) -> None:  # silence per-request logging
        pass

    @property
    def app(self) -> "DashboardServer":
        return self.server.app  # type: ignore[attr-defined]

    # ---- helpers
    def _send(self, status: int, body: bytes, ctype: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, status: int = 200) -> None:
        self._send(status, json.dumps(obj, default=str).encode(), "application/json")

    # ---- routes
    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self._send(200, (_STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif url.path == "/api/state":
            self._json(self.app.controller.snapshot())
        elif url.path == "/api/events":
            q = parse_qs(url.query)
            since = int(q.get("since", ["0"])[0])
            limit = int(q.get("limit", ["200"])[0])
            self._json([e.to_dict() for e in self.app.net.bus.history(since, limit)])
        elif url.path == "/api/stream":
            self._stream()
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/command":
            self._json({"error": "not found"}, 404)
            return
        # Requiring a JSON content type forces a CORS preflight, which blocks
        # other websites from driving a locally running demo.
        if "application/json" not in (self.headers.get("Content-Type") or ""):
            self._json({"ok": False, "error": "Content-Type must be application/json"}, 415)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("body must be a JSON object")
        except ValueError as exc:
            self._json({"ok": False, "error": f"bad request: {exc}"}, 400)
            return
        ctl = self.app.controller
        res = ctl.run_line(str(body["line"])) if "line" in body else ctl.execute(body)
        self._json(res, 200 if res.get("ok") else 400)

    def _stream(self) -> None:
        q: "queue.Queue[dict]" = queue.Queue(maxsize=2000)

        def on_event(ev: Any) -> None:
            try:
                q.put_nowait(ev.to_dict())
            except queue.Full:  # slow client: it will resync from the 1.5 s poll
                pass

        unsubscribe = self.app.net.bus.subscribe(on_event)  # subscribe first -> no gap
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self._sse({"type": "snapshot", "state": self.app.controller.snapshot()})
            last_write = time.monotonic()
            while not self.app.stopping.is_set():
                try:
                    self._sse(q.get(timeout=1.0))
                    last_write = time.monotonic()
                except queue.Empty:
                    if time.monotonic() - last_write > 10:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        last_write = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            unsubscribe()

    def _sse(self, obj: Any) -> None:
        self.wfile.write(b"data: " + json.dumps(obj, default=str).encode() + b"\n\n")
        self.wfile.flush()


class DashboardServer:
    """Serve the live dashboard for a network.

    >>> srv = DashboardServer(net, host="127.0.0.1", port=8765).start()
    >>> print(srv.url)
    """

    def __init__(self, net: Network, controller: Optional[Controller] = None,
                 host: str = "127.0.0.1", port: int = 8765) -> None:
        self.net = net
        self.controller = controller or Controller(net)
        self.stopping = threading.Event()
        self.httpd = ThreadingHTTPServer((host, port), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.app = self  # type: ignore[attr-defined]
        self._thread: Optional[threading.Thread] = None

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    @property
    def url(self) -> str:
        host = self.httpd.server_address[0]
        return f"http://{'127.0.0.1' if host in ('0.0.0.0', '') else host}:{self.port}/"

    def start(self) -> "DashboardServer":
        """Serve in a background thread and return immediately."""
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="domainnet-http",
                                        daemon=True)
        self._thread.start()
        return self

    def serve_forever(self) -> None:
        """Serve in the current thread (blocks until ``stop()``)."""
        self.httpd.serve_forever()

    def stop(self) -> None:
        self.stopping.set()
        self.httpd.shutdown()
        self.httpd.server_close()
