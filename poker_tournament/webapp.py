"""The browser app: watch bots play, play against them, and look at tournament results.

    poker watch      # bots at one table, cards face up
    poker play       # you against them
    poker results    # leaderboards from results/

Everything runs on this computer (http://127.0.0.1:8790 by default) using only
the standard library: the page polls /api/state, and the table itself is a
TableSession from arena.py.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from .arena import SessionConfig, TableSession
from .tournament import summarize_saved

WEB = Path(__file__).parent / "web"
PAGES = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}
MAX_BODY = 64 * 1024


class App:
    """What the web pages talk to: the current table, the bots available and saved results."""

    def __init__(
        self,
        available: dict[str, Path],
        builtin: set[str],
        config: SessionConfig,
        results_dir: Path | None = None,
        hand_log: str | Path | None = None,
    ):
        self.available = available
        self.builtin = builtin
        self.results_dir = Path(results_dir) if results_dir else None
        self.hand_log = hand_log
        self._lock = threading.Lock()
        self.session: TableSession | None = None
        self.start(config)

    def start(self, config: SessionConfig) -> TableSession:
        """Replace the current table with a new one (raises ValueError for bad settings)."""
        session = TableSession(config, self.available, self.hand_log)
        with self._lock:
            old, self.session = self.session, session
        if old:
            old.stop()
        session.start()
        return session

    def stop(self) -> None:
        if self.session:
            self.session.stop()

    def bots(self) -> list[dict]:
        rows = [{"name": name, "builtin": name in self.builtin} for name in self.available]
        return sorted(rows, key=lambda row: (row["builtin"], row["name"].lower()))

    def runs(self) -> list[dict]:
        if not self.results_dir or not self.results_dir.is_dir():
            return []
        runs = []
        for folder in self.results_dir.iterdir():
            done = (folder / "results.json").is_file()
            if not done and not (folder / "run.json").is_file():
                continue
            runs.append({"name": folder.name, "done": done, "modified": (folder / ("results.json" if done else "run.json")).stat().st_mtime})
        return sorted(runs, key=lambda run: run["modified"], reverse=True)

    def run(self, name: str) -> dict:
        if name not in {run["name"] for run in self.runs()}:
            raise LookupError(name)
        folder = self.results_dir / name
        if (folder / "results.json").is_file():
            return dict(json.loads((folder / "results.json").read_text(encoding="utf-8")), done=True)
        return dict(summarize_saved(folder), done=False)


class _Handler(BaseHTTPRequestHandler):
    app: App
    server_version = "PokerTournament"

    def log_message(self, *args) -> None:  # keep the console quiet
        pass

    def do_GET(self) -> None:
        url = urlparse(self.path)
        if url.path in PAGES:
            name, kind = PAGES[url.path]
            return self._send(200, (WEB / name).read_bytes(), f"{kind}; charset=utf-8")
        if url.path == "/api/state":
            query = parse_qs(url.query)
            try:
                since = int(query.get("since", ["-1"])[0])
            except ValueError:
                since = -1
            session = self.app.session
            if query.get("session", [""])[0] != session.id:
                return self._json(session.snapshot())
            return self._json(session.wait(since))
        if url.path == "/api/bots":
            return self._json({"bots": self.app.bots()})
        if url.path == "/api/results":
            return self._json({"runs": self.app.runs()})
        if url.path.startswith("/api/results/"):
            try:
                return self._json(self.app.run(unquote(url.path[len("/api/results/") :])))
            except LookupError:
                return self._json({"error": "no such run"}, 404)
            except (OSError, ValueError, KeyError) as exc:  # e.g. a file being written right now
                return self._json({"error": f"couldn't read that run yet: {exc}"}, 503)
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        url = urlparse(self.path)
        try:
            body = self._body()
            session = self.app.session
            if url.path == "/api/start":
                return self._json(self.app.start(SessionConfig.from_dict(body)).snapshot())
            if url.path == "/api/action":
                session.submit_action(body)
                return self._json({"ok": True})
            if url.path == "/api/control":
                allowed = {"paused", "speed", "step", "next_hand", "show_cards"}
                session.control(**{k: v for k, v in body.items() if k in allowed})
                return self._json({"ok": True})
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        self._json({"error": "not found"}, 404)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("request too large")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            raise ValueError("request isn't valid JSON") from None
        if not isinstance(body, dict):
            raise ValueError("request must be a JSON object")
        return body

    def _json(self, data: dict, status: int = 200) -> None:
        self._send(status, json.dumps(data).encode("utf-8"), "application/json")

    def _send(self, status: int, payload: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # the page was closed or reloaded


def create_server(app: App, port: int = 8790, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """Bind to `port`, or the next free one (port 0 picks any free port)."""
    handler = type("Handler", (_Handler,), {"app": app})
    last_error = None
    for candidate in [port] if port == 0 else range(port, port + 20):
        try:
            server = ThreadingHTTPServer((host, candidate), handler)
            server.daemon_threads = True
            return server
        except OSError as exc:
            last_error = exc
    raise OSError(f"no free port from {port} to {port + 19}: {last_error}")


def serve(app: App, port: int = 8790, open_browser: bool = True, page: str = "") -> None:
    """Run until Ctrl+C."""
    server = create_server(app, port)
    url = f"http://127.0.0.1:{server.server_address[1]}/{page}"
    print(f"Poker table running at {url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        threading.Timer(0.6, webbrowser.open, [url]).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
        app.stop()
