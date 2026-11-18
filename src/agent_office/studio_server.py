"""Loopback HTTP entry point for the user-operated local studio."""
from __future__ import annotations

from http.server import ThreadingHTTPServer
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
from urllib.parse import urlsplit

from .models import OfficeError, validate_id
from .observer import ObserverHandler
from .store import TaskStore
from .studio_catalog import StudioCatalog
from .studio_runner import LocalRunner

ASSETS = {
    "/": ("studio.html", "text/html"),
    "/studio.js": ("studio.js", "text/javascript"),
    "/studio.css": ("studio.css", "text/css"),
}
MAX_BODY = 128 * 1024


class StudioServer(ThreadingHTTPServer):
    # Finish accepting/handling in-flight HTTP requests before releasing runner ownership.
    daemon_threads = False
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, db_path: Path, port: int = 8766):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("Invalid port")
        self.runner = None
        super().__init__(("127.0.0.1", port), StudioHandler)
        try:
            self.db_path = Path(db_path).expanduser().resolve()
            with TaskStore(self.db_path):
                pass
            self.identity = self.db_path.stat()
            self.catalog = StudioCatalog(self.db_path)
            self.runner = LocalRunner(self.db_path)
        except BaseException:
            super().server_close()
            raise

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address

    def server_close(self):
        super().server_close()
        if self.runner is not None:
            self.runner.close()


class StudioHandler(ObserverHandler):
    allowed_methods = "GET, HEAD, POST"

    def _json_body(self):
        size = self.headers.get_all("Content-Length", [])
        if (self.headers.get_all("Transfer-Encoding")
                or self.headers.get_all("Content-Type") != ["application/json"]
                or len(size) != 1 or not re.fullmatch(r"[0-9]{1,6}", size[0])
                or not 0 < int(size[0]) <= MAX_BODY):
            raise ValueError("요청 형식 또는 크기를 확인하세요.")
        body = self.rfile.read(int(size[0]))
        if len(body) != int(size[0]):
            raise ValueError("요청을 전부 받지 못했습니다.")
        try:
            data = json.loads(body.decode("utf-8"))
        except (ValueError, RecursionError):
            raise ValueError("JSON 입력을 확인하세요.") from None
        if not isinstance(data, dict):
            raise ValueError("JSON 객체를 입력하세요.")
        return data

    def _dispatch(self, write=False):
        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise ValueError("요청 주소를 확인하세요.")
        if not self._allowed(True):
            self._error(403, "FORBIDDEN")
            return
        if write:
            host = self.headers["Host"].removesuffix(":80")
            if (self.headers.get_all("Origin") != [f"http://{host}"]
                    or self.headers.get_all("X-Agent-Office") != ["studio"]):
                self._error(403, "FORBIDDEN")
                return
        if parsed.query and not (not write and parsed.path == "/" and
                                 re.fullmatch(r"run=[a-f0-9]{32}", parsed.query)):
            raise ValueError("요청 주소를 확인하세요.")
        if not write and parsed.path in ASSETS:
            name, mime = ASSETS[parsed.path]
            self._reply(200, files("agent_office").joinpath("web", name).read_bytes(), mime)
            return
        if not os.path.samestat(self.server.db_path.stat(), self.server.identity):
            raise OfficeError("STORE_IO")
        catalog, runner = self.server.catalog, self.server.runner
        route = re.fullmatch(r"/api/runs/([a-f0-9]{32})(/stop)?", parsed.path)
        if not write:
            if parsed.path == "/api/workspaces":
                self._reply(200, {"workspaces": catalog.list()})
            elif parsed.path == "/api/runs":
                self._reply(200, {"runs": runner.runs()})
            elif route and not route[2]:
                self._reply(200, {"run": runner.detail(route[1])})
            else:
                self._error(404, "NOT_FOUND")
            return
        data = self._json_body()
        if parsed.path == "/api/workspaces":
            if set(data) != {"name", "paths"}:
                raise ValueError("작업공간 이름과 폴더를 입력하세요.")
            self._reply(201, {"workspace": catalog.create(data["name"], data["paths"])})
        elif parsed.path == "/api/runs":
            if set(data) != {"id", "workspace_id", "indices", "task", "allow_edits"}:
                raise ValueError("작업 요청의 입력 항목을 확인하세요.")
            run_id = validate_id(data["id"])
            workspace_id = validate_id(data["workspace_id"])
            task = data["task"]
            if (type(data["allow_edits"]) is not bool or not isinstance(task, str)
                    or not task.strip() or len(task.encode("utf-8")) > 8192 or "\x00" in task):
                raise ValueError("작업 내용(UTF-8 최대 8,192바이트)과 수정 모드를 확인하세요.")
            run = runner.submit(catalog, run_id, workspace_id, data["indices"], task, data["allow_edits"])
            self._reply(202, {"run": run})
        elif route and route[2] and not data:
            self._reply(200, {"run": runner.stop(route[1])})
        else:
            self._error(404, "NOT_FOUND")

    def _handle(self, write=False):
        try:
            self._dispatch(write)
        except (BrokenPipeError, ConnectionResetError):
            return
        except (ValueError, OverflowError) as exc:
            self._reply(400, {"error": {"code": "INVALID_ARGUMENT", "message": str(exc)[:300]}})
        except KeyError:
            self._error(404, "NOT_FOUND")
        except OfficeError as exc:
            if exc.code == "NOT_FOUND":
                self._error(404, "NOT_FOUND")
            elif exc.code == "RUN_BUSY":
                self._reply(409, {"error": {"code": exc.code, "message": "실행 상태를 확인하고 다시 시도하세요."}})
            elif exc.code == "REQUEST_CONFLICT":
                self._reply(409, {"error": {"code": exc.code, "message": "같은 실행 ID에 다른 요청이 등록되어 있습니다."}})
            else:
                self._error(503, "STORE_IO")
        except (sqlite3.Error, OSError):
            self._error(503, "STORE_IO")

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle(write=True)

    def _write_not_allowed(self):
        self._reply(405, {"error": {"code": "METHOD_NOT_ALLOWED", "message": "지원하지 않는 요청입니다."}})

    do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _write_not_allowed


def serve_studio(db_path: Path, port: int):
    with StudioServer(db_path, port) as server:
        print(f"Agent Office studio: http://127.0.0.1:{server.server_address[1]}/", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
