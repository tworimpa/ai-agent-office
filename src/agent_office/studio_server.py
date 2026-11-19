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
from urllib.parse import parse_qsl, urlsplit
import uuid

from .models import OfficeError, validate_id
from .engines import DEFAULT_DEVELOPER, DEFAULT_REVIEWER
from .engine_evidence import StudioEngineCatalog
from .observer import ObserverHandler
from .store import TaskStore
from .studio_catalog import StudioCatalog
from .studio_runner import LocalRunner
from .away_ledger import return_report, revoke_policy
from .studio_ssh import RemoteUnavailable, is_peer_route, remote_request

ASSETS = {
    "/": ("studio.html", "text/html"),
    "/studio.js": ("studio.js", "text/javascript"),
    "/studio.css": ("studio.css", "text/css"),
}
MAX_BODY = 128 * 1024


def _journal_id(store):
    # This identifier travels with the remote run journal across process restarts.
    # It identifies the selected store. SSH provides transport authentication.
    with store._write():
        store.connection.execute("CREATE TABLE IF NOT EXISTS studio_identity "
                                 "(singleton INTEGER PRIMARY KEY CHECK(singleton=1), id TEXT NOT NULL)")
        store.connection.execute("INSERT OR IGNORE INTO studio_identity VALUES(1,?)", (uuid.uuid4().hex,))
        return validate_id(store.connection.execute("SELECT id FROM studio_identity WHERE singleton=1").fetchone()[0])


def _valid_page_query(query):
    pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=2)
    params = dict(pairs)
    return (len(pairs) == len(params) and not params.keys() - {"host", "run"}
            and all(re.fullmatch(r"[a-f0-9]{32}", value) for value in params.values()))


class StudioServer(ThreadingHTTPServer):
    # Finish accepting/handling in-flight HTTP requests before releasing runner ownership.
    daemon_threads = False
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, db_path: Path, port: int = 8766, *, remotes=(), accept_remote=False):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("Invalid port")
        self.runner = None
        self.engines = None
        self.remotes = {host.journal_id: host for host in remotes}
        if len(self.remotes) != len(remotes) or len({h.alias.casefold() for h in remotes}) != len(remotes):
            raise ValueError("각 원격 호스트의 SSH 별칭과 저널 ID는 고유해야 합니다.")
        self.accept_remote = accept_remote
        super().__init__(("127.0.0.1", port), StudioHandler)
        try:
            self.db_path = Path(db_path).expanduser().resolve()
            with TaskStore(self.db_path) as store:
                self.journal_id = _journal_id(store)
            if self.journal_id in self.remotes:
                raise ValueError("이 컴퓨터의 저널 ID를 원격 호스트로 등록할 수 없습니다.")
            self.identity = self.db_path.stat()
            self.catalog = StudioCatalog(self.db_path)
            self.engines = StudioEngineCatalog(self.db_path)
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
        if self.engines is not None:
            self.engines.close()
        if self.runner is not None:
            self.runner.close()


class StudioHandler(ObserverHandler):
    allowed_methods = "GET, HEAD, POST"

    def end_headers(self):
        if getattr(self, "_peer_reply", False):
            self.send_header("X-Agent-Office-Journal", self.server.journal_id)
        super().end_headers()

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
        if parsed.query and not (not write and parsed.path == "/" and _valid_page_query(parsed.query)):
            raise ValueError("요청 주소를 확인하세요.")
        if not write and parsed.path in ASSETS:
            name, mime = ASSETS[parsed.path]
            self._reply(200, files("agent_office").joinpath("web", name).read_bytes(), mime)
            return
        if not os.path.samestat(self.server.db_path.stat(), self.server.identity):
            raise OfficeError("STORE_IO")
        if parsed.path == "/api/hosts" and not write:
            hosts = [{"id": "local", "name": "이 컴퓨터"}]
            hosts.extend({"id": host.journal_id, "name": host.alias} for host in self.server.remotes.values())
            self._reply(200, {"hosts": hosts})
            return
        path = parsed.path
        target = re.fullmatch(r"/api/(hosts|peer)/([a-f0-9]{32})(/.*)", path)
        if target:
            kind, journal_id, route = target.groups()
            method = "POST" if write else "GET"
            if not is_peer_route(method, route):
                self._error(404, "NOT_FOUND")
                return
            if kind == "hosts":
                self._forward(journal_id, method, route)
                return
            if not self.server.accept_remote:
                self._reply(403, {"error": {"code": "PEER_DISABLED", "message": "원격 실행 접수가 꺼져 있습니다."}})
                return
            if journal_id != self.server.journal_id:
                self._reply(409, {"error": {"code": "JOURNAL_MISMATCH", "message": "등록한 원격 실행 저널과 다릅니다."}})
                return
            self._peer_reply = True
            path = "/api" + route
        self._local_api(path, write)

    def _forward(self, journal_id, method, route):
        host = self.server.remotes.get(journal_id)
        if host is None:
            self._reply(404, {"error": {"code": "UNKNOWN_HOST", "message": "등록되지 않은 원격 호스트입니다."}})
            return
        payload = self._json_body() if method == "POST" else None
        status, result = remote_request(host, method, route, payload)
        if status >= 400:
            error = result.get("error")
            if not isinstance(error, dict) or not isinstance(error.get("code"), str):
                raise RemoteUnavailable()
            if error["code"] in {"PEER_DISABLED", "JOURNAL_MISMATCH"}:
                status = 503
        self._reply(status, result)

    def _local_api(self, path, write):
        catalog, runner = self.server.catalog, self.server.runner
        route = re.fullmatch(r"/api/runs/([a-f0-9]{32})(/stop)?", path)
        inbox_route = re.fullmatch(r"/api/runs/([a-f0-9]{32})/inbox(?:/([a-f0-9]{32})/decision)?", path)
        revoke = re.fullmatch(r"/api/runs/([a-f0-9]{32})/delegation/stop", path)
        if not write:
            if path == "/api/return-report":
                self._reply(200, return_report(self.server.db_path))
            elif path == "/api/inbox":
                self._reply(200, {"requests": runner.inbox.pending()})
            elif inbox_route and not inbox_route[2]:
                self._reply(200, {"requests": runner.inbox.history(inbox_route[1])})
            elif path == "/api/engines":
                self._reply(200, self.server.engines.snapshot())
            elif path == "/api/workspaces":
                self._reply(200, {"workspaces": catalog.list()})
            elif path == "/api/runs":
                self._reply(200, {"runs": runner.runs()})
            elif route and not route[2]:
                self._reply(200, {"run": runner.detail(route[1])})
            else:
                self._error(404, "NOT_FOUND")
            return
        data = self._json_body()
        if path == "/api/workspaces":
            if set(data) != {"name", "paths"}:
                raise ValueError("작업공간 이름과 폴더를 입력하세요.")
            self._reply(201, {"workspace": catalog.create(data["name"], data["paths"])})
        elif path == "/api/runs":
            required_keys = {"id", "workspace_id", "indices", "task", "allow_edits"}
            if not required_keys <= data.keys() or data.keys() - required_keys - {"developer_engine", "reviewer_engine", "use_inbox", "delegation"}:
                raise ValueError("작업 요청의 입력 항목을 확인하세요.")
            run_id = validate_id(data["id"])
            workspace_id = validate_id(data["workspace_id"])
            task = data["task"]
            if (type(data["allow_edits"]) is not bool or not isinstance(task, str)
                    or not task.strip() or len(task.encode("utf-8")) > 8192 or "\x00" in task):
                raise ValueError("작업 내용(UTF-8 최대 8,192바이트)과 수정 모드를 확인하세요.")
            run = runner.submit(catalog, run_id, workspace_id, data["indices"], task, data["allow_edits"],
                                developer_engine=data.get("developer_engine", DEFAULT_DEVELOPER),
                                reviewer_engine=data.get("reviewer_engine", DEFAULT_REVIEWER),
                                use_inbox=data.get("use_inbox", False), delegation=data.get("delegation"))
            self._reply(202, {"run": run})
        elif revoke and not data:
            self._reply(200, revoke_policy(self.server.db_path, revoke[1]))
        elif inbox_route and inbox_route[2]:
            self._reply(200, {"request": runner.inbox.decide(inbox_route[1], inbox_route[2], data)})
        elif route and route[2] and not data:
            self._reply(200, {"run": runner.stop(route[1])})
        else:
            self._error(404, "NOT_FOUND")

    def _handle(self, write=False):
        try:
            self._dispatch(write)
        except (BrokenPipeError, ConnectionResetError):
            return
        except RemoteUnavailable:
            self._reply(503, {"error": {"code": "REMOTE_UNAVAILABLE",
                                        "message": "원격 작업실과 연결할 수 없습니다. 연결이 복구되면 같은 실행을 확인하세요."}})
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
                self._reply(409, {"error": {"code": exc.code, "message": "같은 ID에 다른 요청 또는 결정이 이미 기록되어 있습니다."}})
            elif exc.code == "REQUEST_CLOSED":
                self._reply(409, {"error": {"code": exc.code, "message": "이 요청의 대기가 끝났습니다. 결정 내역을 확인하세요."}})
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


def serve_studio(db_path: Path, port: int, *, remotes=(), accept_remote=False):
    with StudioServer(db_path, port, remotes=remotes, accept_remote=accept_remote) as server:
        print(f"Agent Office studio: http://127.0.0.1:{server.server_address[1]}/", flush=True)
        if accept_remote:
            print(f"Remote studio port: {server.server_address[1]} | Journal ID: {server.journal_id}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
