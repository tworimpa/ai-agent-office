"""Loopback HTTP entry point for the user-operated local studio."""
from __future__ import annotations

from http.server import ThreadingHTTPServer
from importlib.resources import files
import json
import os
from pathlib import Path
import re
import threading
import socket
import sqlite3
from urllib.parse import parse_qsl, urlsplit
import uuid

from .models import OfficeError, validate_id
from .engines import DEFAULT_DEVELOPER, DEFAULT_REVIEWER, resolve_executable
from .engine_evidence import StudioEngineCatalog
from .observer import ObserverHandler
from .office_state import office_snapshot
from .office_wire import office_offset
from . import improvement_api
from .improvement_wire import improvement_route
from .version_wire import version_route
from . import deployment_view
from .studio_improvements import StudioImprovements
from .store import TaskStore
from .studio_catalog import StudioCatalog
from .role_skills import RoleSkillRegistry
from .studio_runner import LocalRunner
from .away_ledger import return_report, revoke_policy
from .studio_ssh import ROLE_BOT_RECEIPT_HEADER, RemoteUnavailable, is_peer_route, legacy_role_bot_reply, remote_request
from . import worktree_storage
from .workspace_rooms import WorkspaceRooms, room_route
from . import room_bundle
from . import github_studio
from . import workspace_briefing, work_profile_views
from . import web_bot_api
from .github_connection import GitHubError
from . import overnight_goals

ASSETS = {
    "/studio-shared.js": ("studio-shared.js", "text/javascript"),
    "/workspace-seats.js": ("workspace-seats.js", "text/javascript"),
    "/workspace-refresh.js": ("workspace-refresh.js", "text/javascript"),
    "/workspace-summary.js": ("workspace-summary.js", "text/javascript"),

    "/engine-maintenance.js": ("engine-maintenance.js", "text/javascript"),
    "/workspace-progress.js": ("workspace-progress.js", "text/javascript"),
    "/": ("workspace-office.html", "text/html"),
    "/workspace-storage.js": ("workspace-storage.js", "text/javascript"),
    "/workspace-office.css": ("workspace-office.css", "text/css"),
    "/workspace-bootstrap.js": ("workspace-bootstrap.js", "text/javascript"),
    "/workspace-office.js": ("workspace-office.js", "text/javascript"),
    "/workspace-goal.js": ("workspace-goal.js", "text/javascript"),
    "/workspace-briefing.js": ("workspace-briefing.js", "text/javascript"),
    "/workspace-bots.js": ("workspace-bots.js", "text/javascript"),
    "/workspace": ("workspace-office.html", "text/html"),
    "/bots": ("bot-client.html", "text/html"),
    "/bot-client.css": ("bot-client.css", "text/css"),
    "/bot-room-scene.css": ("bot-room-scene.css", "text/css"),
    **{"/" + name: (name, "text/javascript") for name in (
        "bot-client.js", "bot-client-ui.js", "bot-client-files.js", "bot-client-orders.js", "bot-room-scene.js")},
    **{"/bot-art/" + name: ("bot-art/" + name, "image/webp") for name in (
        *(f"{kind}-{i}.webp" for kind in ("figure", "walk") for i in range(12)),
        "study-seated-king.webp", "office.webp")},
    "/workspace-profile.js": ("workspace-profile.js", "text/javascript"),
    "/workspace-character.js": ("workspace-character.js", "text/javascript"),
    "/workspace-github.js": ("workspace-github.js", "text/javascript"),
    "/office-content.js": ("office-content.js", "text/javascript"),
    "/office-content.css": ("office-content.css", "text/css"),
    "/office-flow.js": ("office-flow.js", "text/javascript"),
    "/mermaid-flow-renderer.js": ("mermaid-flow-renderer.js", "text/javascript"),
    "/office-flow.css": ("office-flow.css", "text/css"),
    "/workspace-study.webp": ("workspace-study.webp", "image/webp"),
    "/workspace-portraits.webp": ("workspace-portraits.webp", "image/webp"),
    "/studio": ("studio.html", "text/html"),
    "/studio.js": ("studio.js", "text/javascript"),
    "/studio.css": ("studio.css", "text/css"),
    "/office": ("office.html", "text/html"),
    "/office.css": ("office.css", "text/css"),
    "/office.js": ("office.js", "text/javascript"),
    "/office-agents.webp": ("office-agents.webp", "image/webp"),
    "/improvements": ("improvements.html", "text/html"),
    "/improvements.js": ("improvements.js", "text/javascript"),
    "/improvements.css": ("improvements.css", "text/css"),
    "/versions": ("versions.html", "text/html"),
    "/versions.js": ("versions.js", "text/javascript"),
    "/versions.css": ("versions.css", "text/css"),
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


def _valid_page_query(path, query):
    keys = {"host", "run", "workspace"} if path in ("/", "/workspace") else {"host", "run"}
    pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=len(keys))
    params = dict(pairs)
    return (len(pairs) == len(params) and params.keys() <= keys
            and all(re.fullmatch(r"[a-f0-9]{32}", value) or (key == "host" and value == "local")
                    for key, value in pairs))


def _bot_page_query(query):
    pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=3)
    params = dict(pairs)
    return (len(pairs) == len(params) and params.keys() <= {"workspace", "bot", "run"}
            and all(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", value) and len(value) <= 64 if key == "bot"
                    else re.fullmatch(r"[a-f0-9]{32}", value) for key, value in pairs))


def _improvements_query(query):
    pairs = parse_qsl(query, keep_blank_values=True, strict_parsing=True, max_num_fields=2)
    params = dict(pairs)
    return (len(params) == len(pairs) and not params.keys() - {"host", "job", "candidate"}
            and not {"job", "candidate"} <= params.keys()
            and all(re.fullmatch(r"[a-f0-9]{32}", v) or (k == "host" and v == "local")
                    for k, v in pairs))


class StudioServer(ThreadingHTTPServer):
    # Finish accepting/handling in-flight HTTP requests before releasing runner ownership.
    daemon_threads = False
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()

    def __init__(self, db_path: Path, port: int = 8766, *, remotes=(), accept_remote=False,
                 improvement_source=None, release_view=None, deployment_target=None, expected_journal=None,
                 standalone=False):
        if type(port) is not int or not 0 <= port <= 65535:
            raise ValueError("Invalid port")
        self.progress_slots = threading.BoundedSemaphore(8)
        self.progress_closed = threading.Event()
        self.runner = None
        self.github = None
        self.engines = None
        self.improvements = None
        self.improvement_source = improvement_source
        self.release_view = release_view
        self.deployment_target = Path(deployment_target).expanduser().resolve() if deployment_target else None
        self.remotes = {host.journal_id: host for host in remotes}
        if len(self.remotes) != len(remotes) or len({h.alias.casefold() for h in remotes}) != len(remotes):
            raise ValueError("각 원격 호스트의 SSH 별칭과 저널 ID는 고유해야 합니다.")
        self.accept_remote = accept_remote
        super().__init__(("127.0.0.1", port), StudioHandler)
        try:
            self.db_path = Path(db_path).expanduser().resolve()
            if expected_journal is None:
                mode = os.environ.get("AGENT_OFFICE_SQLITE_JOURNAL", "wal") if standalone else "delete"
                if mode not in {"wal", "delete"}:
                    raise ValueError("AGENT_OFFICE_SQLITE_JOURNAL must be wal or delete")
                with TaskStore(self.db_path, wal_on_create=mode == "wal") as store:
                    self.journal_id = _journal_id(store)
            else:
                # Restoring a saved Desktop must never initialize a replacement journal.
                with TaskStore(self.db_path, readonly=True) as store:
                    row = store.connection.execute("SELECT id FROM studio_identity WHERE singleton=1").fetchone()
                    if not row or row[0] != validate_id(expected_journal):
                        raise ValueError("저장된 작업실 저널을 확인하세요.")
                    self.journal_id = expected_journal
            if self.journal_id in self.remotes:
                raise ValueError("이 컴퓨터의 저널 ID를 원격 호스트로 등록할 수 없습니다.")
            self.identity = self.db_path.stat()
            self.catalog = StudioCatalog(self.db_path)
            self.skills = RoleSkillRegistry(self.db_path)
            self.engines = StudioEngineCatalog(self.db_path)
            self.runner = LocalRunner(self.db_path)
            from .goal_peer import configure
            configure(self.db_path, remotes)
            self.directions = self.runner.directions
            self.rooms = WorkspaceRooms(self.db_path, self.catalog, self.runner, self.engines, journal_id=self.journal_id)
            self.improvements = StudioImprovements(self.db_path, improvement_source)
            self.github = github_studio.WorkspaceGitHub(self)
        except BaseException:
            self.server_close()
            raise

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address

    def server_close(self):
        self.progress_closed.set()
        super().server_close()
        if self.github is not None:
            self.github.close()
        if self.improvements is not None:
            self.improvements.close()
        if self.engines is not None:
            self.engines.close()
        if self.runner is not None:
            self.runner.close()


class StudioHandler(ObserverHandler):
    allowed_methods = "GET, HEAD, POST"

    def _reply(self, status, body, mime="application/json"):
        if (getattr(self, "_peer_reply", False) and status < 300
                and self.headers.get_all(ROLE_BOT_RECEIPT_HEADER) != ["1"]):
            body = legacy_role_bot_reply(body)
        try:
            super()._reply(status, body, mime)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # Navigating away or superseding a poll closes its response socket.
            self.close_connection = True

    def end_headers(self):
        if getattr(self, "_peer_reply", False):
            self.send_header("X-Agent-Office-Journal", self.server.journal_id)
        super().end_headers()

    def _json_body(self, limit=MAX_BODY, *, unique=False):
        size = self.headers.get_all("Content-Length", [])
        if (self.headers.get_all("Transfer-Encoding")
                or self.headers.get_all("Content-Type") != ["application/json"]
                or len(size) != 1 or not re.fullmatch(r"[0-9]{1,6}", size[0])
                or not 0 < int(size[0]) <= limit):
            raise ValueError("요청 형식 또는 크기를 확인하세요.")
        body = self.rfile.read(int(size[0]))
        if len(body) != int(size[0]):
            raise ValueError("요청을 전부 받지 못했습니다.")
        try:
            if unique:
                from .bot_spec import unique_object
                data = json.loads(body.decode("utf-8"), object_pairs_hook=unique_object)
            else:
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
        # Closing with unread body bytes can reset the connection and discard
        # the rejection response on Windows. Read once after the origin checks.
        manifest_import = bool(re.fullmatch(r"/api/rooms/[0-9a-f]{32}/bots/import", parsed.path))
        payload = self._json_body(512 * 1024 if manifest_import else MAX_BODY, unique=manifest_import) if write else None
        expected = self.headers.get_all("X-Agent-Office-Expected-Journal")
        if expected is not None and expected != [self.server.journal_id]:
            self._reply(409, {"error": {"code": "JOURNAL_MISMATCH", "message": "선택한 작업실과 다른 저널입니다."}})
            return
        # Older Studios reject API queries. A bound URL therefore also fails closed
        # if an older process takes over this port before the next identity check.
        binding = re.fullmatch(r"office_journal=([0-9a-f]{32})", parsed.query)
        if binding and parsed.path.startswith("/api/"):
            if binding[1] != self.server.journal_id:
                self._reply(409, {"error": {"code": "JOURNAL_MISMATCH", "message": "선택한 작업실과 다른 저널입니다."}})
                return
            parsed = parsed._replace(query="")
        if parsed.query:
            studio_page = parsed.path in ("/", "/workspace", "/studio") and _valid_page_query(parsed.path, parsed.query)
            office_page = parsed.path in ("/office", "/versions") and re.fullmatch(r"host=(?:local|[a-f0-9]{32})", parsed.query)
            improvements_page = parsed.path == "/improvements" and _improvements_query(parsed.query)
            bot_page = parsed.path == "/bots" and _bot_page_query(parsed.query)
            if write or not (studio_page or office_page or improvements_page or bot_page):
                raise ValueError("요청 주소를 확인하세요.")
        if not write and parsed.path in ASSETS:
            # Keep existing execution links and the first-time Workspace registration flow.
            page = "/bots" if parsed.path == "/" and not parsed.query and self.server.catalog.list() else parsed.path
            name, mime = ASSETS[page]
            self._reply(200, files("agent_office").joinpath("web", name).read_bytes(), mime)
            return
        if not os.path.samestat(self.server.db_path.stat(), self.server.identity):
            raise OfficeError("STORE_IO")
        if parsed.path == "/api/hosts" and not write:
            hosts = [{"id": "local", "name": "이 컴퓨터"}]
            hosts.extend({"id": host.journal_id, "name": host.alias} for host in self.server.remotes.values())
            self._reply(200, {"hosts": hosts})
            return
        if parsed.path == "/api/studio-identity" and not write:
            self._reply(200, {"journal_id": self.server.journal_id, "journal_guard": True})
            return
        if parsed.path == "/api/studio-tools" and not write:
            git = "git.exe" if os.name == "nt" else "git"
            self._reply(200, {"git": {"installed": resolve_executable(git) is not None}})
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
                self._forward(journal_id, method, route, payload)
                return
            if not self.server.accept_remote:
                self._reply(403, {"error": {"code": "PEER_DISABLED", "message": "원격 실행 접수가 꺼져 있습니다."}})
                return
            if journal_id != self.server.journal_id:
                self._reply(409, {"error": {"code": "JOURNAL_MISMATCH", "message": "등록한 원격 실행 저널과 다릅니다."}})
                return
            self._peer_reply = True
            path = "/api" + route
        self._local_api(path, payload)

    def _forward(self, journal_id, method, route, payload):
        host = self.server.remotes.get(journal_id)
        if host is None:
            self._reply(404, {"error": {"code": "UNKNOWN_HOST", "message": "등록되지 않은 원격 호스트입니다."}})
            return
        status, result = remote_request(host, method, route, payload)
        if status >= 400:
            error = result.get("error")
            if not isinstance(error, dict) or not isinstance(error.get("code"), str):
                raise RemoteUnavailable()
            if error["code"] in {"PEER_DISABLED", "JOURNAL_MISMATCH"}:
                status = 503
        self._reply(status, result)

    def _local_api(self, path, data):
        write = data is not None
        if not getattr(self, "_peer_reply", False):
            response = web_bot_api.handle(self.server, path, data)
            if response is not None:
                self._reply(*response)
                return
        bundle = room_bundle.route("POST" if write else "GET", path.removeprefix("/api"))
        if bundle:
            self._reply(200, room_bundle.read(self.server, *bundle))
            return
        maintenance = re.fullmatch(r"/api/engines/(codex|claude|antigravity|gemini|cline)/check", path)
        if maintenance and write and not getattr(self, "_peer_reply", False):
            from .engine_updates import check
            if data:
                raise ValueError("버전 확인 요청을 확인하세요.")
            self._reply(200, {**check(maintenance[1]), "journal_id": self.server.journal_id})
            return
        progress = re.fullmatch(r"/api/runs/([a-f0-9]{32})/stream", path)
        if progress and not write and not getattr(self, "_peer_reply", False):
            from .run_progress import stream
            cursor = self.headers.get("Last-Event-ID", "0")
            if not re.fullmatch(r"[0-9]{1,18}", cursor):
                raise ValueError("진행 기록 번호를 확인하세요.")
            stream(self, progress[1], int(cursor))
            return
        storage = worktree_storage.route("POST" if write else "GET", path.removeprefix("/api"))
        if storage:
            self._reply(200, worktree_storage.handle(self.server, storage, data))
            return
        profile = work_profile_views.route("POST" if write else "GET", path.removeprefix("/api"))
        if profile:
            self._reply(200, work_profile_views.handle(self.server, *profile, data))
            return
        briefing = workspace_briefing.route("POST" if write else "GET", path.removeprefix("/api"))
        if briefing:
            self._reply(200, workspace_briefing.handle(self.server, *briefing, data))
            return
        integration = github_studio.route("POST" if write else "GET", path.removeprefix("/api"))
        if integration:
            self._reply(*self.server.github.handle(*integration, data))
            return
        catalog, runner = self.server.catalog, self.server.runner
        peer_check = re.fullmatch(r"/api/goal-checks/([a-f0-9]{32})", path)
        if peer_check:
            from .goal_peer import lookup
            self._reply(200, lookup(self.server.db_path, peer_check[1], data))
            return
        room = room_route("POST" if write else "GET", path.removeprefix("/api")) if path.startswith("/api/") else None
        if room:
            workspace_id, view, cursor = room
            if view == "assignments":
                self._reply(202, {"run": self.server.rooms.assign(workspace_id, data)})
            elif view == "inbox":
                self._reply(200, {"requests": self.server.rooms.inbox(workspace_id)})
            elif view == "goal-policy":
                self._reply(200, {"policy": self.server.rooms.goal_policy(workspace_id, data)})
            elif write:
                self._reply(200, {"settings": self.server.rooms.save(workspace_id, data)})
            else:
                self._reply(200, self.server.rooms.snapshot(workspace_id, cursor))
            return
        goal = overnight_goals.goal_route("POST" if write else "GET", path.removeprefix("/api"))
        if goal:
            run_id, action, attempt = goal
            if action == "recover":
                from .goal_recovery import reconcile
                if set(data) != {"id"}:
                    raise ValueError("복구 요청 ID를 확인하세요.")
                result = {"recovery": reconcile(runner, run_id, validate_id(data["id"])), "run_id": run_id}
            elif action == "adopt":
                result = overnight_goals.adopt(self.server.db_path, run_id, data)
            elif action == "candidate":
                result = overnight_goals.candidate(self.server.db_path, run_id, attempt)
            elif action == "compare":
                result = overnight_goals.compare(self.server.db_path, run_id, attempt)
            else:
                result = {"goal": overnight_goals.report(self.server.db_path, run_id)}
            self._reply(200, result)
            return
        meeting_gate = re.fullmatch(r"/api/runs/([a-f0-9]{32})/meeting/decision", path)
        if meeting_gate and data is not None:
            self._reply(200, {"meeting": self.server.runner.decide_meeting(meeting_gate[1], data)})
            return
        directions = re.fullmatch(r"/api/runs/([a-f0-9]{32})/directions", path)
        if directions:
            run_id = directions[1]
            if write:
                self._reply(202, {"direction": self.server.directions.submit(run_id, data)})
            else:
                self._reply(200, {"directions": self.server.directions.history(run_id)})
            return
        if path.startswith("/api/deployment"):
            route = deployment_view.deployment_route("POST" if write else "GET", path.removeprefix("/api"))
            if route is None:
                self._error(404, "NOT_FOUND")
            else:
                try:
                    status = deployment_view.read_status(self.server.deployment_target, self.server.release_view,
                                                         self.server.journal_id, route)
                    deployment_view.check_reply(200, status, route)
                except (ValueError, KeyError, TypeError):
                    raise OfficeError("STORE_IO") from None
                self._reply(200, status)
            return
        if path.startswith("/api/versions"):
            from . import stable_view
            target = version_route("POST" if write else "GET", path.removeprefix("/api"))
            if target is None:
                self._error(404, "NOT_FOUND")
            else:
                lookup = stable_view.versions if target[0] == "page" else stable_view.compare
                self._reply(200, lookup(self.server.release_view, self.server.journal_id, *target[1:]))
            return
        if path.startswith("/api/improvements"):
            target = improvement_route("POST" if write else "GET", path.removeprefix("/api"))
            if target is None:
                self._error(404, "NOT_FOUND")
            else:
                status, data = improvement_api.dispatch(self.server, target, data)
                # Earlier SSH clients validate the exact candidate schema.
                if (target[0] in ("candidate", "candidates") and getattr(self, "_peer_reply", False)
                        and self.headers.get("X-Agent-Office-Candidate-Basis") != "1"):
                    rows = [data["candidate"]] if target[0] == "candidate" else data["items"]
                    for row in rows:
                        row.pop("stale_base", None)
                self._reply(status, data)
            return
        if not write and path.startswith("/api/"):
            offset = office_offset(path.removeprefix("/api"))
            if offset is not None:
                self._reply(200, office_snapshot(self.server.db_path, offset=offset))
                return
        route = re.fullmatch(r"/api/runs/([a-f0-9]{32})(/stop)?", path)
        inbox_route = re.fullmatch(r"/api/runs/([a-f0-9]{32})/inbox(?:/([a-f0-9]{32})/decision)?", path)
        revoke = re.fullmatch(r"/api/runs/([a-f0-9]{32})/delegation/stop", path)
        skill_route = re.fullmatch(r"/api/skills/(?:(before)/)?([a-f0-9]{32})", path)
        if not write:
            if path == "/api/skills":
                self._reply(200, self.server.skills.page())
            elif skill_route and skill_route[1]:
                self._reply(200, self.server.skills.page(before=skill_route[2]))
            elif skill_route:
                self._reply(200, {"skill": self.server.skills.detail(skill_route[2])})
            elif path == "/api/return-report":
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
        if path == "/api/skills":
            if set(data) != {"name", "instructions"}:
                raise ValueError("스킬 이름과 Markdown 지침을 입력하세요.")
            self._reply(201, {"skill": self.server.skills.create(data["name"], data["instructions"])})
        elif path == "/api/workspaces":
            if set(data) != {"name", "paths"}:
                raise ValueError("작업공간 이름과 폴더를 입력하세요.")
            self._reply(201, {"workspace": catalog.create(data["name"], data["paths"])})
        elif path == "/api/runs":
            required_keys = {"id", "workspace_id", "indices", "task", "allow_edits"}
            optional_keys = {"developer_engine", "reviewer_engine", "use_inbox", "delegation", "role_skills", "subtasks"}
            if not required_keys <= data.keys() or data.keys() - required_keys - optional_keys:
                raise ValueError("작업 요청의 입력 항목을 확인하세요.")
            if ("role_skills" in data and not isinstance(data["role_skills"], dict)
                    or "subtasks" in data and not isinstance(data["subtasks"], list)):
                raise ValueError("역할별 스킬과 하위 작업 입력을 확인하세요.")
            run_id = validate_id(data["id"])
            workspace_id = validate_id(data["workspace_id"])
            task = data["task"]
            if (type(data["allow_edits"]) is not bool or not isinstance(task, str)
                    or not task.strip() or len(task.encode("utf-8")) > 8192 or "\x00" in task):
                raise ValueError("작업 내용(UTF-8 최대 8,192바이트)과 수정 모드를 확인하세요.")
            run = runner.submit(catalog, run_id, workspace_id, data["indices"], task, data["allow_edits"],
                                developer_engine=data.get("developer_engine", DEFAULT_DEVELOPER),
                                reviewer_engine=data.get("reviewer_engine", DEFAULT_REVIEWER),
                                use_inbox=data.get("use_inbox", False), delegation=data.get("delegation"),
                                role_skills=data.get("role_skills"), subtasks=data.get("subtasks"))
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
        except GitHubError as exc:
            self._reply(503, {"error":{"code": exc.code, "message": "GitHub 연결 오류 · " + exc.code}})
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


def serve_studio(db_path: Path, port: int, *, remotes=(), accept_remote=False,
                 improvement_source=None, release_view=None, deployment_target=None):
    with StudioServer(db_path, port, remotes=remotes, accept_remote=accept_remote,
                      improvement_source=improvement_source, release_view=release_view,
                      deployment_target=deployment_target, standalone=deployment_target is None) as server:
        print(f"Agent Office studio: http://127.0.0.1:{server.server_address[1]}/", flush=True)
        if accept_remote:
            print(f"Remote studio port: {server.server_address[1]} | Journal ID: {server.journal_id}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
