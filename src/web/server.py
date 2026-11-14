"""A small local web server for the dashboard and the settings page.

It listens on 127.0.0.1 only, so nothing on your network (or the internet)
can reach it. It also refuses requests whose Host header is not localhost,
which blocks DNS-rebinding tricks from web pages you have open, and only
accepts JSON for changes, which a cross-site form cannot send.
"""
from __future__ import annotations

import csv
import json
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .. import config as config_mod
from ..engine import KILL_FILE
from ..strategy.registry import catalog

STATIC = Path(__file__).parent / "static"
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                 ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml"}
MAX_BODY = 256 * 1024


class App:
    """What the request handler needs: where logs live and which run is live."""

    def __init__(self, logs_dir: Path, config_path: Path, live_run: Path | None = None):
        self.logs_dir = Path(logs_dir)
        self.config_path = Path(config_path)
        self.live_run = Path(live_run) if live_run else None

    def runs(self) -> list[str]:
        if not self.logs_dir.exists():
            return []
        return sorted((p.name for p in self.logs_dir.iterdir() if (p / "run.json").exists()), reverse=True)

    def run_dir(self, name: str | None) -> Path | None:
        if name:
            return self.logs_dir / name if name in self.runs() else None  # only known folder names
        if self.live_run:
            return self.live_run
        runs = self.runs()
        return self.logs_dir / runs[0] if runs else None


def read_csv(path: Path, limit: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    return rows[-limit:] if limit else rows


def tail_jsonl(path: Path, limit: int) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def thin(rows: list, limit: int) -> list:
    if len(rows) <= limit:
        return rows
    step = len(rows) / limit
    picked = [rows[int(i * step)] for i in range(limit - 1)]
    return picked + [rows[-1]]


def make_handler(app: App, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "trading-bot"

        def log_message(self, *args):  # keep the terminal for trading output
            pass

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, status: int = 200) -> None:
            self._send(status, json.dumps(data, default=str).encode(), "application/json")

        def _host_ok(self) -> bool:
            if self.headers.get("Host", "") not in allowed_hosts:
                self._json({"error": "forbidden host"}, HTTPStatus.FORBIDDEN)
                return False
            return True

        def do_GET(self):
            if not self._host_ok():
                return
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            path = url.path
            if path in ("/", "/index.html"):
                return self._static("index.html")
            if path in ("/settings", "/settings.html"):
                return self._static("settings.html")
            if path.startswith("/static/"):
                return self._static(path.removeprefix("/static/"))
            if path == "/api/runs":
                return self._json({"runs": app.runs(), "live": app.live_run.name if app.live_run else None})
            if path == "/api/config":
                return self._config_get()
            if path.startswith("/api/"):
                run = app.run_dir(q.get("run"))
                if run is None:
                    return self._json({"error": "no run yet"}, HTTPStatus.NOT_FOUND)
                if path == "/api/status":
                    status = run / "status.json"
                    data = json.loads(status.read_text(encoding="utf-8")) if status.exists() else {"state": "starting"}
                    data["run_dir"] = str(run)
                    data["live"] = app.live_run is not None and run == app.live_run
                    return self._json(data)
                if path == "/api/trades":
                    return self._json(read_csv(run / "trades.csv"))
                if path == "/api/equity":
                    return self._json(thin(read_csv(run / "equity.csv"), 1500))
                if path == "/api/bars":
                    return self._json(read_csv(run / "bars.csv", limit=int(q.get("limit", 720))))
                if path == "/api/events":
                    return self._json(tail_jsonl(run / "events.jsonl", int(q.get("limit", 200))))
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self):
            if not self._host_ok():
                return
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                return self._json({"error": "JSON only"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._json({"error": "too large"}, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json({"error": "bad JSON"}, HTTPStatus.BAD_REQUEST)
            path = urlparse(self.path).path
            if path == "/api/config":
                return self._config_post(body)
            if path == "/api/kill":
                if not app.live_run:
                    return self._json({"error": "no live run"}, HTTPStatus.CONFLICT)
                (app.live_run / KILL_FILE).write_text("pressed from dashboard\n")
                return self._json({"ok": True})
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def _static(self, name: str) -> None:
            target = (STATIC / name).resolve()
            if STATIC.resolve() not in target.parents or not target.is_file():
                return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            self._send(200, target.read_bytes(), CONTENT_TYPES.get(target.suffix, "application/octet-stream"))

        def _config_get(self) -> None:
            path = app.config_path
            config_mod.ensure_local_config(path)
            cfg, errors = config_mod.load(path)
            self._json({"config": cfg, "errors": errors, "path": str(path),
                        "privacy": config_mod.git_privacy(path),
                        "catalog": catalog(cfg["strategy"], config_mod.ROOT),
                        "live": app.live_run is not None})

        def _config_post(self, body: dict) -> None:
            cfg, errors = config_mod.validate(body.get("config") or {})
            if errors:
                return self._json({"ok": False, "errors": errors}, HTTPStatus.UNPROCESSABLE_ENTITY)
            config_mod.save(cfg, app.config_path)
            self._json({"ok": True, "path": str(app.config_path),
                        "privacy": config_mod.git_privacy(app.config_path),
                        "catalog": catalog(cfg["strategy"], config_mod.ROOT)})

    return Handler


def free_port(preferred: int) -> int:
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise OSError(f"No free port between {preferred} and {preferred + 19}")


def serve(app: App, port: int, background: bool = True) -> tuple[ThreadingHTTPServer, int]:
    port = free_port(port)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, port))
    server.daemon_threads = True
    if background:
        threading.Thread(target=server.serve_forever, name="dashboard", daemon=True).start()
    return server, port
