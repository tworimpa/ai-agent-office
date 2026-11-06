"""Loopback-only read-only observer. No arbitrary filesystem or execution surface."""
from dataclasses import asdict
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import parse_qs, urlsplit

from .models import OfficeError, MAX_SQLITE_INT, validate_id
from .store import TaskStore

ASSETS = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
          "/state.js": ("state.js", "text/javascript"), "/style.css": ("style.css", "text/css")}
MESSAGES = {"INVALID_ARGUMENT": "Invalid request.", "NOT_FOUND": "Resource not found.",
            "FORBIDDEN": "Request not allowed.", "METHOD_NOT_ALLOWED": "Only GET and HEAD are available.",
            "DB_NOT_FOUND": "Database is unavailable.", "SCHEMA_UNSUPPORTED": "Database schema is unsupported.",
            "DB_BUSY": "Database is busy. Retry later.", "STORE_IO": "Database could not be read."}
CSP = "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"


class ObserverServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, db_path: Path, port: int = 8765):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("Invalid port")
        self.db_path = Path(db_path).expanduser().resolve()
        try:
            self._identity = self.db_path.stat()
        except FileNotFoundError:
            raise OfficeError("DB_NOT_FOUND") from None
        with TaskStore(self.db_path, readonly=True):
            pass
        self._check_identity()
        super().__init__(("127.0.0.1", port), ObserverHandler)

    def _check_identity(self):
        try:
            current = self.db_path.stat()
        except FileNotFoundError:
            raise OfficeError("DB_NOT_FOUND") from None
        if not os.path.samestat(self._identity, current):
            raise OfficeError("STORE_IO")

    @contextmanager
    def read_store(self):
        self._check_identity()
        with TaskStore(self.db_path, readonly=True) as store:
            yield store
            self._check_identity()

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


class ObserverHandler(BaseHTTPRequestHandler):
    server_version = "AgentOffice"
    sys_version = ""

    def log_message(self, *args):
        pass

    def _reply(self, status, body, mime="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        for key, value in {"Content-Type": mime + "; charset=utf-8", "Content-Length": str(len(data)),
                           "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                           "Content-Security-Policy": CSP, "X-Frame-Options": "DENY",
                           "Referrer-Policy": "no-referrer"}.items():
            self.send_header(key, value)
        if status == 405:
            self.send_header("Allow", "GET, HEAD")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _error(self, status, code):
        self._reply(status, {"error": {"code": code, "message": MESSAGES[code]}})

    def _allowed(self, api):
        port = self.server.server_address[1]
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        host_values = self.headers.get_all("Host", [])
        if len(host_values) != 1 or host_values[0] not in hosts:
            return False
        if api:
            origins = self.headers.get_all("Origin", [])
            if len(origins) > 1 or (origins and origins[0] not in {"http://" + host for host in hosts}):
                return False
            if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
                return False
        return True

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc or parsed.fragment:
                raise ValueError("Invalid target")
            api = parsed.path.startswith("/api/")
            if not self._allowed(api):
                self._error(403, "FORBIDDEN")
                return
            params = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=10)
            if any(len(value) != 1 for value in params.values()):
                raise ValueError("Duplicate query")
            if parsed.path in ASSETS:
                if parsed.path == "/" and set(params) == {"task"}:
                    validate_id(params["task"][0])
                elif params:
                    raise ValueError("Unexpected query")
                asset, mime = ASSETS[parsed.path]
                try:
                    data = files("agent_office").joinpath("web", asset).read_bytes()
                except FileNotFoundError:
                    self._error(404, "NOT_FOUND")
                    return
                self._reply(200, data, mime)
                return
            if parsed.path == "/api/meta":
                if params:
                    raise ValueError("Unexpected query")
                with self.server.read_store():
                    pass
                self._reply(200, {"mode": "read-only", "database": self.server.db_path.name, "task_limit": 200})
                return
            if parsed.path == "/api/tasks":
                if params:
                    raise ValueError("Unexpected query")
                with self.server.read_store() as store:
                    result = {"tasks": [asdict(task) for task in store.list_tasks(limit=200)], "limit": 200}
                self._reply(200, result)
                return
            route = re.fullmatch(r"/api/tasks/([^/]+)(/events)?", parsed.path)
            if not route:
                self._error(404, "NOT_FOUND")
                return
            task_id = validate_id(route[1])
            if not route[2]:
                if params:
                    raise ValueError("Unexpected query")
                with self.server.read_store() as store:
                    result = {"task": asdict(store.get_task(task_id))}
            else:
                if set(params) - {"run_id", "after_seq", "limit"}:
                    raise ValueError("Unexpected query")
                run_id = validate_id(params["run_id"][0]) if "run_id" in params else None
                cursor = int(params.get("after_seq", ["0"])[0])
                limit = int(params.get("limit", ["100"])[0])
                if not 0 <= cursor <= MAX_SQLITE_INT or not 1 <= limit <= 500:
                    raise ValueError("Invalid range")
                with self.server.read_store() as store:
                    task = store.get_task(task_id)
                    events = store.list_events(task_id, run_id=run_id, after_seq=cursor, limit=limit+1)
                result = {"task_id": task_id, "run_id": run_id or task.current_run_id,
                          "events": [asdict(event) for event in events[:limit]],
                          "next_after_seq": events[min(len(events), limit)-1].seq if events else cursor,
                          "has_more": len(events) > limit}
            self._reply(200, result)
        except (ValueError, OverflowError):
            self._error(400, "INVALID_ARGUMENT")
        except OfficeError as exc:
            self._error(404 if exc.code == "NOT_FOUND" else 503, exc.code if exc.code in MESSAGES else "STORE_IO")
        except sqlite3.Error as exc:
            code = getattr(exc, "sqlite_errorcode", 0) & 255
            self._error(503, "DB_BUSY" if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) else "STORE_IO")
        except OSError:
            self._error(503, "STORE_IO")

    def _write_not_allowed(self):
        self._error(405, "METHOD_NOT_ALLOWED")

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _write_not_allowed


def serve(db_path: Path, port: int = 8765):
    with ObserverServer(db_path, port) as server:
        print(f"Agent Office observer: http://127.0.0.1:{server.server_address[1]}/ (read-only)", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
