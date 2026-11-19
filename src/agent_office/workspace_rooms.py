"""Workspace defaults and the coordinator's durable assignment receipt."""
from collections import Counter
import json
import platform
import re
import threading
from hashlib import sha256

from . import role_skills
from .away_evidence import DelegationDeferred, git as evidence_git
from .engines import DEFAULT_DEVELOPER, DEFAULT_REVIEWER, SUPPORTED_ENGINES
from .models import OfficeError, validate_id
from .office_state import _latest_stages
from .store import TaskStore, encode
from .studio_catalog import _single_line
from .studio_inbox import ROLE_STATE, holder_alive
from .studio_runner import SOURCE, SUMMARY
from .team_runs import validate_task


ROLES = ("developer", "reviewer")
ROOM_LIMIT = 80
CURSOR_PATTERN = r"[ah]-[0-9a-f]{32}"


def room_route(method, path):
    match = re.fullmatch(rf"/rooms/([0-9a-f]{{32}})(?:/(assignments|inbox)|/history/({CURSOR_PATTERN}))?", path)
    if match:
        workspace, view, cursor = match.groups()
        if method == "GET" and view != "assignments" or method == "POST" and view != "inbox" and cursor is None:
            return workspace, view, cursor
    return None


def default_settings():
    return {"delegation": False,
            "developer": {"name": "개발 담당", "engine": DEFAULT_DEVELOPER, "skills": []},
            "reviewer": {"name": "검토 담당", "engine": DEFAULT_REVIEWER, "skills": []}}


def normalize_profiles(data):
    if not isinstance(data, dict) or set(data) != set(ROLES):
        raise ValueError("개발·검토 담당자 설정을 확인하세요.")
    selection = {}
    profiles = {}
    for role in ROLES:
        profile = data[role]
        if not isinstance(profile, dict) or set(profile) != {"name", "engine", "skills"}:
            raise ValueError("담당자 이름, 엔진, 스킬을 확인하세요.")
        name, engine = profile["name"], profile["engine"]
        if not _single_line(name) or len(name.strip()) > 40 or engine not in SUPPORTED_ENGINES:
            raise ValueError("담당자 이름은 40자 이하로 입력하고 지원 엔진을 선택하세요.")
        selection[role] = profile["skills"]
        profiles[role] = {"name": name.strip(), "engine": engine}
    clean = role_skills.normalize_selection(selection)
    for role, profile in profiles.items():
        profile["skills"] = clean.get(role, [])
    return profiles


def normalize_settings(data):
    if not isinstance(data, dict) or set(data) != {*ROLES, "delegation"} or type(data["delegation"]) is not bool:
        raise ValueError("Workspace의 역할과 기술 판단 위임 기본값을 확인하세요.")
    return {"delegation": data["delegation"], **normalize_profiles({role: data[role] for role in ROLES})}


def _eligible(row, capability):
    return (row["installed"] and row["headless_support"] and row["probe_status"] == "ok"
            and row["auth_status"] != "not_authenticated"
            and row["capabilities"][capability]["status"] != "failed")


def _choose(rows, capability, preferred, *, excluded=None):
    available = [row for row in rows if row["engine"] != excluded and _eligible(row, capability)]
    available.sort(key=lambda row: (row["engine"] != preferred,
                                   row["capabilities"][capability]["status"] != "passed",
                                   row["auth_status"] != "authenticated"))
    if available:
        return available[0]["engine"]
    return None


def _assign_roles(settings, engines, allow_edits):
    roles = {role: dict(settings[role]) for role in ROLES}
    for role, capability in (("developer", "edit" if allow_edits else "read"), ("reviewer", "review")):
        if role == "reviewer" and not allow_edits:
            break
        engine = _choose(engines, capability, roles[role]["engine"])
        if engine is None:
            if any(row["probe_status"] == "pending" for row in engines):
                raise ValueError("호스트의 엔진 연결을 확인 중입니다. 잠시 후 같은 요청을 다시 보내세요.")
            raise ValueError(f"이 호스트에서 {roles[role]['name']} 역할을 실행할 엔진이 없습니다. 엔진 설정을 확인하세요.")
        roles[role]["engine"] = engine
    return roles


def _assignment_note(settings, roles, allow_edits, delegated, choices):
    if allow_edits:
        note = f"{roles['developer']['name']}에게 작업을, {roles['reviewer']['name']}에게 검토를 맡겼습니다."
    else:
        note = f"{roles['developer']['name']}에게 파일 수정 없는 작업을 맡겼습니다."
    active = ROLES if allow_edits else ("developer",)
    if any(roles[role]["engine"] != settings[role]["engine"] for role in active):
        note += " 호스트에서 사용할 수 있는 엔진으로 배정했습니다."
    if delegated and choices is None:
        note += " 이 호스트에서 기술 판단을 검증할 수 없어 사용자의 응답을 받습니다."
    return note


def _requests_by_role(store, ids):
    if not ids:
        return Counter()
    binds = ",".join("?" for _ in ids)
    rows = store.connection.execute(f"""SELECT q.run_id,q.role,q.holder,
        (((p.run_id IS NOT NULL AND p.revoked_at IS NULL) OR
          (cp.run_id IS NOT NULL AND cp.revoked_at IS NULL)) AND c.request_id IS NOT NULL
          AND c.reason IS NULL AND c.evidence IS NULL) AS checking
        FROM studio_requests q
        JOIN runs r ON r.run_id=q.run_id JOIN tasks t ON t.task_id=r.task_id
        JOIN studio_runs s ON s.id=q.run_id
        LEFT JOIN studio_away_checks c ON c.request_id=q.id
        LEFT JOIN studio_away_policies p ON p.run_id=q.run_id
        LEFT JOIN studio_away_choice_policies cp ON cp.run_id=q.run_id
        WHERE q.run_id IN ({binds}) AND q.status='pending' AND q.attempt=r.attempts
          AND r.ended_at IS NULL AND t.current_run_id=q.run_id AND NOT s.stop_requested AND s.proposed_state IS NULL
          AND ((q.role='developer' AND r.state=? AND q.engine=t.developer_engine)
            OR (q.role='reviewer' AND r.state=? AND q.engine=t.reviewer_engine))""",
        (*ids, ROLE_STATE["developer"], ROLE_STATE["reviewer"]))
    return Counter((row["run_id"], row["role"], bool(row["checking"]))
                   for row in rows if holder_alive(store.path, row["holder"]))


def _lines(row, requests, stages):
    state = row["state"]
    working = {"developer": "작업을 진행하고 있습니다.", "reviewer": "변경 사항을 검토하고 있습니다."}
    result = {}
    for role in ROLES:
        stage = stages.get(role)
        if not row["allow_edits"] and role == "reviewer":
            result[role] = ""
        elif state == "STOP_REQUESTED":
            result[role] = "작업을 중지하고 있습니다."
        elif requests[row["id"], role, False]:
            result[role] = "사용자 응답을 기다리고 있습니다."
        elif requests[row["id"], role, True]:
            result[role] = "조정자가 근거를 확인하고 있습니다."
        elif state == ROLE_STATE[role] and stage == "STAGE_STARTED":
            result[role] = working[role]
        elif role == "developer" and state == "TESTING" and stages.get("tester") == "STAGE_STARTED":
            result[role] = "테스트를 실행하고 있습니다."
        elif stage == "STAGE_COMPLETED":
            result[role] = "결과를 남겼습니다."
        elif stage == "STAGE_FAILED":
            result[role] = "이 단계가 실패했습니다."
        elif state in {"PLAN_ONLY", "READY_FOR_HUMAN_REVIEW", "WAITING_USER", "FAILED", "CANCELED", "RECOVERY_REQUIRED"}:
            result[role] = {"FAILED": "작업이 실패했습니다.", "CANCELED": "작업을 중지했습니다.",
                            "RECOVERY_REQUIRED": "중단된 실행을 확인해 주세요."}.get(state, "작업이 종료되었습니다.")
        else:
            result[role] = "작업 시작을 기다리고 있습니다."
    return result


class WorkspaceRooms:
    def __init__(self, db_path, catalog, runner, engines):
        self.path = db_path
        self.catalog = catalog
        self.runner = runner
        self.engines = engines
        self.assignments = threading.Lock()
        with TaskStore(db_path, existing=True) as store, store._write():
            store.connection.execute("""CREATE TABLE IF NOT EXISTS studio_room_settings(
                workspace_id TEXT PRIMARY KEY, settings_json TEXT NOT NULL)""")
            store.connection.execute("CREATE INDEX IF NOT EXISTS studio_runs_workspace ON studio_runs(workspace_id)")

    def _workspace(self, workspace_id):
        return self.catalog.get(workspace_id)

    @staticmethod
    def _settings(db, workspace_id):
        row = db.execute("SELECT settings_json FROM studio_room_settings WHERE workspace_id=?", (workspace_id,)).fetchone()
        return normalize_settings(json.loads(row[0])) if row else default_settings()

    def save(self, workspace_id, data):
        self._workspace(workspace_id)
        settings = normalize_settings(data)
        with TaskStore(self.path, existing=True) as store, store._write():
            db = store.connection
            role_skills.validate_selection(db, {role: settings[role]["skills"] for role in ROLES})
            db.execute("INSERT INTO studio_room_settings VALUES(?,?) ON CONFLICT(workspace_id) "
                       "DO UPDATE SET settings_json=excluded.settings_json", (workspace_id, encode(settings)))
        return settings

    def snapshot(self, workspace_id, cursor=None):
        workspace = self._workspace(workspace_id)
        engines = self.engines.snapshot()["engines"] if cursor is None else None
        with self.runner.lock, TaskStore(self.path, existing=True) as store:
            self.runner._refresh(store)
            db = store.connection
            db.execute("BEGIN")
            scope = (workspace_id, platform.node())
            before, params = "", ()
            if cursor is not None:
                if not re.fullmatch(CURSOR_PATTERN, cursor):
                    raise ValueError("작업 기록 위치를 확인하세요.")
                anchor = db.execute("SELECT r.started_at FROM runs r JOIN studio_runs s ON s.id=r.run_id "
                                    "WHERE s.workspace_id=? AND r.host_name=? AND r.run_id=?",
                                    (*scope, cursor[2:])).fetchone()
                if anchor is None:
                    raise OfficeError("NOT_FOUND")
                before = " AND (r.ended_at IS NULL,r.started_at,r.run_id) < (?,?,?)"
                params = (int(cursor.startswith("a-")), anchor[0], cursor[2:])
            rows = db.execute(SUMMARY + ",a.assignment_json,r.ended_at IS NULL AS active" + SOURCE + """
                LEFT JOIN studio_room_assignments a ON a.run_id=s.id
                WHERE s.workspace_id=? AND r.host_name=?""" + before + " ORDER BY active DESC,r.started_at DESC,r.run_id DESC LIMIT ?",
                (*scope, *params, ROOM_LIMIT + 1)).fetchall()
            more = len(rows) > ROOM_LIMIT
            rows = rows[:ROOM_LIMIT]
            ids = [row["id"] for row in rows]
            stages = _latest_stages(db, ids) if ids else {}
            requests = _requests_by_role(store, ids)
            runs = []
            for row in rows:
                run = self.runner._summary(row)
                assignment = run.pop("assignment_json")
                run.pop("active")
                pending = sum(requests[row["id"], role, False] for role in ROLES)
                run.update(assignment=json.loads(assignment) if assignment else None, pending_count=pending,
                           lines=_lines(row, requests, stages[row["id"]]))
                runs.append(run)
            last = rows[-1] if more else None
            next_cursor = ("a-" if last["active"] else "h-") + last["id"] if last else None
            page = {"runs": runs, "next_cursor": next_cursor}
            if cursor is None:
                page.update(workspace=workspace, settings=self._settings(db, workspace_id), engines=engines)
        return page

    def inbox(self, workspace_id):
        self._workspace(workspace_id)
        return self.runner.inbox.pending(workspace_id=workspace_id)

    def assign(self, workspace_id, data):
        required = {"id", "message", "allow_edits", "delegation"}
        if not required <= data.keys() or data.keys() - required - {"indices"}:
            raise ValueError("조정자에게 보낼 요청을 확인하세요.")
        run_id = validate_id(data["id"])
        task, allow_edits, override = data["message"], data["allow_edits"], data["delegation"]
        validate_task(task)
        if type(allow_edits) is not bool or (override is not None and type(override) is not bool):
            raise ValueError("파일 수정과 기술 판단 위임 옵션을 확인하세요.")
        digest = "room:" + sha256(encode([workspace_id, data]).encode("utf-8")).hexdigest()
        with self.assignments:
            with self.runner.lock, TaskStore(self.path, existing=True) as store:
                self.runner._refresh(store)
                prior = self.runner._previous(store, run_id, digest)
                if prior:
                    return prior
                settings = self._settings(store.connection, workspace_id)
            workspace = self._workspace(workspace_id)
            indices = data.get("indices", list(range(len(workspace["folders"]))))
            repo, folders = self.catalog.select(workspace_id, indices)
            engines = self.engines.snapshot()["engines"]
            roles = _assign_roles(settings, engines, allow_edits)
            delegated = settings["delegation"] if override is None else override
            try:
                revision = evidence_git(repo, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
            except (DelegationDeferred, UnicodeError):
                raise ValueError("작업에 사용할 기준 커밋을 확인할 수 없습니다.") from None
            policy = choices = None
            if delegated:
                from .coordinator_policy import policy_for_assignment

                specialist = _choose(engines, "read", roles["developer"]["engine"])
                verifier = _choose(engines, "read", roles["reviewer"]["engine"], excluded=specialist) or specialist
                if specialist and verifier:
                    choices = (specialist, verifier)
                    policy = policy_for_assignment(repo, revision, folders, specialist_engine=specialist,
                                                   verifier_engine=verifier)
            assignment = {"roles": roles, "delegation": delegated,
                          "note": _assignment_note(settings, roles, allow_edits, delegated, choices)}
            return self.runner.start(run_id, workspace_id, repo, folders, task, allow_edits, indices=indices,
                                     request_digest=digest, developer_engine=roles["developer"]["engine"],
                                     reviewer_engine=roles["reviewer"]["engine"], use_inbox=True,
                                     delegation=policy, role_skills={role: roles[role]["skills"] for role in ROLES},
                                     room_assignment=assignment, room_revision=revision, room_choices=choices)
