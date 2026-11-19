"""Persistent, locally owned Studio executions using the existing task journal."""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import logging
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import threading
from types import SimpleNamespace
import uuid

from . import role_skills as skills
from . import team_runs
from .engines import DEFAULT_DEVELOPER, DEFAULT_REVIEWER, SUPPORTED_ENGINES
from .events import snapshot_request
from .models import ACTIVE, OfficeError, OwnerInfo, TaskSpec, validate_id
from .ownership import RunGuard, RunBusyError, recover_run
from .store import TaskStore, encode
from .studio_diff import changes_in
from .studio_directions import DirectionChannel, Directions
from .studio_inbox import Inbox, InboxChannel, expire_requests
from .away_evidence import DelegationDeferred, git as evidence_git
from .away_expert import RULES, in_scope, validate_policy
from .away_ledger import get_choices, get_policy, get_revision, register_choices, register_policy
from .technical_choices import CHOICE_INSTRUCTIONS
from .workspace import map_folders, plan_worktree


SCHEMA = """CREATE TABLE IF NOT EXISTS studio_runs(
    id TEXT PRIMARY KEY REFERENCES runs(run_id), workspace_id TEXT NOT NULL,
    folders TEXT NOT NULL, allow_edits INTEGER NOT NULL, request_hash TEXT NOT NULL,
    stop_requested INTEGER NOT NULL DEFAULT 0, developer_output TEXT NOT NULL DEFAULT '',
    test_output TEXT NOT NULL DEFAULT '', review_output TEXT NOT NULL DEFAULT '',
    changes TEXT NOT NULL DEFAULT '', diff TEXT NOT NULL DEFAULT '', diagnostic TEXT NOT NULL DEFAULT '',
    proposed_state TEXT, proposed_outcome TEXT, preview TEXT)"""
SUMMARY = """SELECT s.id,s.workspace_id,t.request_snapshot AS task,
    CASE WHEN s.stop_requested AND r.ended_at IS NULL THEN 'STOP_REQUESTED' ELSE r.state END AS state,
    r.outcome,r.task_id,t.workspace,t.branch,t.developer_engine,t.reviewer_engine,r.attempts,r.started_at AS created_at,
    t.updated_at,s.allow_edits,EXISTS(SELECT 1 FROM studio_interactive_runs i WHERE i.id=s.id) AS use_inbox,
    (SELECT policy_json FROM studio_away_policies p WHERE p.run_id=s.id) AS delegation,
    (r.ended_at IS NULL AND s.stop_requested=0 AND s.proposed_state IS NULL AND t.current_run_id=s.id AND
     (EXISTS(SELECT 1 FROM studio_away_policies p WHERE p.run_id=s.id AND p.revoked_at IS NULL) OR
      EXISTS(SELECT 1 FROM studio_away_choice_policies c WHERE c.run_id=s.id AND c.revoked_at IS NULL))) AS delegation_active"""
SOURCE = """ FROM studio_runs s JOIN runs r ON r.run_id=s.id JOIN tasks t ON t.task_id=r.task_id"""
DETAIL = ",s.folders,s.developer_output,s.test_output,s.review_output,s.changes,s.diff,s.diagnostic"


def _request_digest(parts, developer_engine, reviewer_engine, use_inbox=False, delegation=None, role_skills=None, subtasks=None):
    if developer_engine not in SUPPORTED_ENGINES or reviewer_engine not in SUPPORTED_ENGINES:
        raise ValueError("지원하는 개발자·리뷰어 엔진을 선택하세요.")
    # Default-role requests must match the exact digest persisted by Stage 1/2.
    if (developer_engine, reviewer_engine) != (DEFAULT_DEVELOPER, DEFAULT_REVIEWER):
        parts = [*parts, developer_engine, reviewer_engine]
    if type(use_inbox) is not bool:
        raise ValueError("요청함 사용 설정을 확인하세요.")
    if use_inbox:
        parts = [*parts, {"use_inbox": True}]
    if delegation is not None:
        if not use_inbox:
            raise ValueError("부재중 위임에는 협업 요청함이 필요합니다.")
        parts = [*parts, {"delegation": delegation}]
    if role_skills:
        parts = [*parts, {"role_skills": role_skills}]
    if subtasks is not None:
        parts = [*parts, {"subtasks": subtasks}]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def finish_drained(store, run_id, *, worker_succeeded, preview_finished):
    """Publish the result after all workers exit. Stop and finish race in SQLite."""
    with store._write():
        expire_requests(store.connection, run_id)
        if store.get_run(run_id).state not in ACTIVE:
            return
        row = store.connection.execute(
            "SELECT stop_requested, proposed_state, proposed_outcome, preview FROM studio_runs WHERE id=?", (run_id,)
        ).fetchone()
        if row["stop_requested"]:
            state, outcome = "CANCELED", "CANCELED"
        elif worker_succeeded and row["proposed_state"]:
            state, outcome = row["proposed_state"], row["proposed_outcome"]
        else:
            state, outcome = "RECOVERY_REQUIRED", "INTERRUPTED"
        changes, diff = (json.loads(row["preview"]) if preview_finished and row["preview"]
                         else ("(Git preview unavailable)", ""))
        store.connection.execute(
            "UPDATE studio_runs SET changes=?,diff=?,preview=NULL,proposed_state=NULL,proposed_outcome=NULL WHERE id=?",
            (changes, diff, run_id),
        )
        store._record(run_id, operation_key=f"{run_id}:studio:final",
                      event_type="RECOVERY_REQUIRED" if state == "RECOVERY_REQUIRED" else "RUN_FINISHED",
                      state=state, outcome=outcome, payload={"phase": "run"})


class LocalRunner:
    def __init__(self, db_path: Path):
        self.path = Path(db_path).expanduser().resolve()
        self.lock = threading.RLock()
        self.children = {}
        self.pending = {}  # Raw prompts live only in this owner's memory. Never replay on startup.
        self.team_workers = set()
        self.scheduler = None
        self.shutdown = threading.Event()
        self.stale = set()
        self.closed = False
        self.guard = RunGuard.studio(self.path)
        self.guard.__enter__()
        try:
            with TaskStore(self.path) as store:
                store.connection.execute(SCHEMA)
                store.connection.execute("CREATE TABLE IF NOT EXISTS studio_interactive_runs "
                                         "(id TEXT PRIMARY KEY REFERENCES studio_runs(id))")
                store.connection.execute("""CREATE TABLE IF NOT EXISTS studio_room_assignments(
                    run_id TEXT PRIMARY KEY REFERENCES studio_runs(id), assignment_json TEXT NOT NULL,
                    revision TEXT NOT NULL)""")
                self.inbox = Inbox(self.path)
                self.directions = Directions(self.path)
                team_runs.ensure_schema(store.connection)
                skills.ensure_schema(store.connection)
                stale = store.connection.execute(
                    "SELECT r.run_id FROM runs r JOIN studio_runs s ON s.id=r.run_id "
                    "WHERE r.host_name=? AND r.ended_at IS NULL", (platform.node(),)
                ).fetchall()
                for row in stale:
                    if not self._recover(store, row[0]):
                        self.stale.add(row[0])
                        if store.connection.execute("SELECT 1 FROM studio_team_children WHERE id=?", (row[0],)).fetchone():
                            self.team_workers.add(row[0])
            if self.stale:
                self._start_monitor()
        except BaseException:
            self.guard.__exit__(None, None, None)
            raise

    @staticmethod
    def _recover(store, run_id):
        try:
            if store.get_run(run_id).state not in ACTIVE:
                return True
            run = recover_run(store, run_id, host_name=platform.node(), studio_owner=True)
            with store._write():
                expire_requests(store.connection, run_id)
            if run.state != "RECOVERY_REQUIRED":
                return True
            store.connection.execute(
                "UPDATE studio_runs SET diagnostic=? WHERE id=? AND diagnostic=''",
                ("실행기 연결이 종료되었습니다. 마지막 작업 상태를 확인하세요.", run_id),
            )
            return True
        except RunBusyError:
            return False  # A previous owner's supervisor is still draining its children.

    def _refresh(self, store):
        for run_id in self.stale.copy():
            if self._recover(store, run_id):
                self.stale.remove(run_id)
                self.team_workers.discard(run_id)
        for run_id, child in list(self.children.items()):
            if child.poll() is not None:
                child.stdin.close()
                if self._recover(store, run_id):
                    del self.children[run_id]
                    self.team_workers.discard(run_id)

    def _previous(self, store, run_id, digest):
        row = store.connection.execute("SELECT request_hash FROM studio_runs WHERE id=? UNION ALL "
                                       "SELECT request_hash FROM studio_teams WHERE id=?", (run_id, run_id)).fetchone()
        if row is None:
            if store.connection.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone():
                raise OfficeError("REQUEST_CONFLICT")
            return None
        if row[0] != digest:
            raise OfficeError("REQUEST_CONFLICT")
        return self._detail(store, run_id)

    def submit(self, catalog, run_id, workspace_id, indices, task, allow_edits, *,
               developer_engine=DEFAULT_DEVELOPER, reviewer_engine=DEFAULT_REVIEWER, use_inbox=False,
               delegation=None, role_skills=None, subtasks=None):
        # Check the receipt before re-opening folders: the original checkout may have moved.
        policy = validate_policy(delegation) if delegation is not None else None
        selected = skills.normalize_selection({} if role_skills is None else role_skills)
        plan = (team_runs.normalize_subtasks(subtasks, indices, developer_engine, reviewer_engine)
                if subtasks is not None else None)
        digest = _request_digest([workspace_id, indices, task, allow_edits], developer_engine, reviewer_engine,
                                 use_inbox, policy, selected, plan)
        with self.lock, TaskStore(self.path, existing=True) as store:
            self._refresh(store)
            existing = self._previous(store, run_id, digest)
            if existing:
                return existing
        repo, folders = catalog.select(workspace_id, indices)
        return self.start(run_id, workspace_id, repo, folders, task, allow_edits, request_digest=digest,
                          developer_engine=developer_engine, reviewer_engine=reviewer_engine, use_inbox=use_inbox,
                          delegation=policy, role_skills=selected, indices=indices, subtasks=plan)

    def start(self, run_id: str, workspace_id: str, repo: Path, folders: list[str],
              task: str, allow_edits: bool, *, request_digest=None, developer_engine=DEFAULT_DEVELOPER,
              reviewer_engine=DEFAULT_REVIEWER, use_inbox=False, delegation=None, role_skills=None,
              indices=None, subtasks=None, room_assignment=None, room_revision=None, room_choices=None):
        validate_id(run_id)
        team_runs.validate_task(task)
        if type(allow_edits) is not bool or not folders or not all(isinstance(f, str) for f in folders):
            raise ValueError("Invalid Studio request")
        repo = Path(repo).resolve()
        indices = list(range(len(folders))) if indices is None else indices
        folder_map = dict(zip(indices, folders, strict=True))
        policy = validate_policy(delegation) if delegation is not None else None
        selected = skills.normalize_selection({} if role_skills is None else role_skills)
        plan = (team_runs.normalize_subtasks(subtasks, indices, developer_engine, reviewer_engine)
                if subtasks is not None else None)
        request_parts = [workspace_id, str(repo), folders, task, allow_edits]
        if plan:
            request_parts.append({"indices": indices})
        digest = request_digest or _request_digest(request_parts, developer_engine, reviewer_engine,
                                                   use_inbox, policy, selected, plan)
        with self.lock, TaskStore(self.path, existing=True) as store:
            if self.closed:
                raise OfficeError("RUN_BUSY")
            self._refresh(store)
            existing = self._previous(store, run_id, digest)
            if existing:
                return existing
            map_folders(repo, folders)
            revision = room_revision
            if policy or plan:
                try:
                    if revision is None:
                        revision = evidence_git(repo, "rev-parse", "--verify", "HEAD^{commit}").decode("ascii").strip()
                    if plan:
                        for folder in folders:
                            spec = f"{revision}^{{tree}}" if folder == "." else f"{revision}:{folder}"
                            if evidence_git(repo, "cat-file", "-t", spec).strip() != b"tree":
                                raise ValueError("기준 커밋에 포함된 작업 폴더를 선택하세요.")
                except (DelegationDeferred, UnicodeError):
                    raise ValueError("작업에 사용할 기준 커밋을 확인할 수 없습니다.") from None

            def add_run(child_id, goal, scope, developer, reviewer, request_hash):
                if policy and (not use_inbox or not all(in_scope(rule["path"], scope) for rule in policy["rules"])):
                    raise ValueError("요청함을 켜고 각 작업 폴더 또는 저장소 루트의 근거 파일을 지정하세요.")
                spec = TaskSpec(uuid.uuid4().hex, str(repo), goal, developer, reviewer, 2)
                store._create_run(spec, child_id, plan_worktree(repo, child_id),
                                  OwnerInfo(platform.node(), uuid.uuid4().hex))
                db = store.connection
                db.execute("INSERT INTO studio_runs(id,workspace_id,folders,allow_edits,request_hash) VALUES (?,?,?,?,?)",
                           (child_id, workspace_id, json.dumps(scope, ensure_ascii=False), allow_edits, request_hash))
                if use_inbox:
                    db.execute("INSERT INTO studio_interactive_runs VALUES (?)", (child_id,))
                if policy:
                    register_policy(db, child_id, policy, revision)
                skills.pin_selection(db, child_id, selected)

            queued = {}
            # Hold self.lock across registration and launch so the monitor sees
            # committed work and never tries to recover the supervisor handoff.
            self._start_monitor()
            with ExitStack() as guards:
                with store._write():
                    if plan:
                        team_runs.register(store.connection, run_id, digest, workspace_id, task, indices, folders,
                                           allow_edits, developer_engine, reviewer_engine, use_inbox, policy, revision)
                        skills.pin_selection(store.connection, run_id, selected)
                        for position, item in enumerate(plan):
                            child_id = uuid.uuid4().hex
                            scope = [folder_map[index] for index in item["indices"]]
                            add_run(child_id, item["task"], scope, item["developer_engine"], item["reviewer_engine"],
                                    f"child:{run_id}:{position}")
                            store.connection.execute("INSERT INTO studio_team_children VALUES (?,?,?,?)",
                                                     (child_id, run_id, position, json.dumps(item["indices"])))
                            guard = guards.enter_context(RunGuard(self.path, child_id))
                            prompt = ("Use the original request for context and shared constraints. "
                                      "Complete and verify only the assigned subtask in this worktree.\n\n"
                                      f"Original request:\n{task}\n\nAssigned subtask:\n{item['task']}")
                            queued[child_id] = (prompt, guard)
                    else:
                        add_run(run_id, task, folders, developer_engine, reviewer_engine, digest)
                        if room_assignment is not None:
                            store.connection.execute("INSERT INTO studio_room_assignments VALUES(?,?,?)",
                                                     (run_id, encode(room_assignment), room_revision))
                            register_choices(store.connection, run_id, room_revision, room_choices)
                # Pending entries take ownership only after the entire team registration commits.
                guards.pop_all()

            if plan:
                self.pending.update(queued)
            else:
                self._launch(store, run_id, task)
            return self._detail(store, run_id)

    def _launch(self, store, run_id, prompt):
        try:
            child = subprocess.Popen(
                [sys.executable, "-m", "agent_office.studio_process", str(self.path), run_id],
                cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            self.children[run_id] = child
            child.stdin.write(json.dumps(prompt, ensure_ascii=False).encode("utf-8") + b"\n")
            child.stdin.flush()
        except OSError:
            if run_id in self.children:
                self.children[run_id].stdin.close()
            else:
                self.stale.add(run_id)
                if self._recover(store, run_id):
                    self.stale.remove(run_id)

    def _schedule(self, store):
        for child_id in list(self.pending):
            if len(self.team_workers) >= team_runs.MAX_ACTIVE:
                break
            state = store.get_run(child_id).state
            prompt, guard = self.pending.pop(child_id)
            try:
                guard.__exit__(None, None, None)
                if state == "QUEUED":
                    self._launch(store, child_id, prompt)
            finally:
                if child_id in self.children:
                    self.team_workers.add(child_id)
                else:
                    self.stale.add(child_id)

    def _start_monitor(self):
        if self.scheduler is None or not self.scheduler.is_alive():
            self.scheduler = threading.Thread(target=self._monitor, daemon=True)
            self.scheduler.start()

    def _monitor(self):
        interval = 0.15
        while not self.shutdown.wait(interval):
            with self.lock:
                if self.closed or not (self.pending or self.children or self.stale):
                    self.scheduler = None
                    return
                can_dispatch = self.pending and len(self.team_workers) < team_runs.MAX_ACTIVE
                if not (self.stale or can_dispatch or any(child.poll() is not None for child in self.children.values())):
                    continue
                try:
                    with TaskStore(self.path, existing=True) as store:
                        self._refresh(store)
                        self._schedule(store)
                    interval = 0.15
                except Exception as exc:
                    if (isinstance(exc, sqlite3.OperationalError)
                            and getattr(exc, "sqlite_errorcode", 0) & 255 in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)):
                        continue
                    # Drop undispatched prompts and recover their journal rows
                    # on a later owner tick. Recovery never relaunches a child.
                    self.stale.update(self.pending)
                    for _, guard in self.pending.values():
                        guard.__exit__(None, None, None)
                    self.pending.clear()
                    if interval == 0.15:
                        logging.getLogger(__name__).exception("Studio owner monitor interrupted")
                    interval = 2.0

    def _detail(self, store, run_id):
        db = store.connection
        parent = db.execute("SELECT * FROM studio_teams WHERE id=?", (run_id,)).fetchone()
        if parent:
            result = team_runs.describe(db, parent, detail=True)
        else:
            row = db.execute(SUMMARY + DETAIL + SOURCE + " WHERE s.id=?", (run_id,)).fetchone()
            if row is None:
                raise OfficeError("NOT_FOUND")
            result = self._summary(row)
            result["folders"] = json.loads(row["folders"])
            relation = db.execute("""SELECT p.id AS parent_id,p.task AS original_task,c.indices,p.base_revision
                FROM studio_team_children c JOIN studio_teams p ON p.id=c.parent_id WHERE c.id=?""",
                (run_id,)).fetchone()
            if relation:
                result.update(dict(relation), indices=json.loads(relation["indices"]), kind="subtask")
            elif result["delegation"]:
                result["base_revision"] = get_revision(self.path, run_id)
            assigned = db.execute("SELECT assignment_json,revision FROM studio_room_assignments WHERE run_id=?",
                                  (run_id,)).fetchone()
            if assigned:
                result.update(assignment=json.loads(assigned[0]), base_revision=assigned[1])
        versions = skills.selected_metadata(db, run_id)
        result["role_skill_snapshots"] = versions
        result["role_skills"] = {role: [version["id"] for version in items] for role, items in versions.items()}
        return result

    @staticmethod
    def _summary(row):
        return dict(row, allow_edits=bool(row["allow_edits"]), use_inbox=bool(row["use_inbox"]),
                    delegation=json.loads(row["delegation"]) if row["delegation"] else None,
                    delegation_active=bool(row["delegation_active"]))

    def detail(self, run_id):
        validate_id(run_id)
        with self.lock, TaskStore(self.path, existing=True) as store:
            self._refresh(store)
            return self._detail(store, run_id)

    def runs(self):
        with self.lock, TaskStore(self.path, existing=True) as store:
            self._refresh(store)
            db = store.connection
            rows = db.execute(SUMMARY + SOURCE + " WHERE NOT EXISTS "
                              "(SELECT 1 FROM studio_team_children c WHERE c.id=s.id) "
                              "ORDER BY r.started_at DESC LIMIT 200")
            result = [self._summary(row) for row in rows]
            parents = db.execute("SELECT * FROM studio_teams ORDER BY created_at DESC LIMIT 200")
            result.extend(team_runs.describe(db, parent, detail=False) for parent in parents)
            return sorted(result, key=lambda item: item["created_at"], reverse=True)[:200]

    def _mark_stopped(self, store, run_id):
        db = store.connection
        if store.get_run(run_id).state not in ACTIVE:
            return
        db.execute("UPDATE studio_runs SET stop_requested=1 WHERE id=?", (run_id,))
        expire_requests(db, run_id)
        if run_id in self.pending:
            store._record(run_id, operation_key=f"{run_id}:studio:final", event_type="RUN_FINISHED",
                          state="CANCELED", outcome="CANCELED", payload={"phase": "run"})

    def stop(self, run_id):
        validate_id(run_id)
        with self.lock, TaskStore(self.path, existing=True) as store:
            self._refresh(store)
            parent = store.connection.execute("SELECT * FROM studio_teams WHERE id=?", (run_id,)).fetchone()
            if parent:
                if parent["host_name"] != platform.node():
                    raise OfficeError("HOST_MISMATCH")
                targets = [row[0] for row in store.connection.execute(
                    "SELECT id FROM studio_team_children WHERE parent_id=?", (run_id,))]
            else:
                if not store.connection.execute("SELECT 1 FROM studio_runs WHERE id=?", (run_id,)).fetchone():
                    raise OfficeError("NOT_FOUND")
                targets = [run_id]
                if store.get_run(run_id).host_name != platform.node():
                    raise OfficeError("HOST_MISMATCH")
            with store._write():
                if parent and any(store.get_run(child).state in ACTIVE for child in targets):
                    store.connection.execute("UPDATE studio_teams SET stop_requested=1 WHERE id=?", (run_id,))
                for child in targets:
                    self._mark_stopped(store, child)
            for child in targets:
                if child in self.pending:
                    _, guard = self.pending.pop(child)
                    guard.__exit__(None, None, None)
                elif child in self.children:
                    self.children[child].stdin.close()
                else:
                    self._recover(store, child)
            return self._detail(store, run_id)

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            self.shutdown.set()
            # Register all pipe closes before touching SQLite. Even if a stop
            # marker cannot be saved, EOF still makes every supervisor drain.
            with ExitStack() as cleanup:
                cleanup.callback(self.guard.__exit__, None, None, None)
                for child in self.children.values():
                    cleanup.callback(child.wait, timeout=30)
                for child in self.children.values():
                    cleanup.callback(child.stdin.close)
                for _, guard in self.pending.values():
                    cleanup.callback(guard.__exit__, None, None, None)
                with TaskStore(self.path, existing=True) as store, store._write():
                    for run_id in (*self.children, *self.pending):
                        self._mark_stopped(store, run_id)
                self.pending.clear()
            with TaskStore(self.path, existing=True) as store:
                self._refresh(store)


def worker(db_path, run_id):
    # Gated by the supervisor: no Git or vendor subprocess before ownership is attached.
    raw_task = json.loads(sys.stdin.buffer.read())
    os.environ["PYTHONIOENCODING"] = "utf-8"  # Fixed pytest command writes UTF-8 to captured pipes.
    from .cli import execute_run

    with TaskStore(db_path, existing=True) as store:
        run = store.get_run(run_id)
        task = store.get_task(run.task_id)
        row = store.connection.execute("SELECT folders,allow_edits FROM studio_runs WHERE id=?", (run_id,)).fetchone()
        interactive = store.connection.execute("SELECT 1 FROM studio_interactive_runs WHERE id=?", (run_id,)).fetchone()
        policy = get_policy(db_path, run_id)
        team = store.connection.execute("""SELECT p.base_revision FROM studio_team_children c
            JOIN studio_teams p ON p.id=c.parent_id WHERE c.id=?""", (run_id,)).fetchone()
        room = store.connection.execute("SELECT revision FROM studio_room_assignments WHERE run_id=?", (run_id,)).fetchone()
        source = team or room
        revision = source[0] if source else get_revision(db_path, run_id)
        args = SimpleNamespace(repo=Path(task.project), task=raw_task, developer=task.developer_engine,
                               reviewer=task.reviewer_engine, max_attempts=task.max_attempts,
                               timeout=900, test_command=[sys.executable, "-m", "pytest", "-q"],
                               allow_edits=bool(row["allow_edits"]))

        def save_stage(column, value):
            if column not in {"developer_output", "test_output", "review_output"}:
                raise ValueError("Unknown output stage")
            store.connection.execute(f"UPDATE studio_runs SET {column}=? WHERE id=?",
                                     (snapshot_request(value), run_id))

        def defer_final(state, outcome):
            store.connection.execute("UPDATE studio_runs SET proposed_state=?,proposed_outcome=? WHERE id=?",
                                     (state, outcome, run_id))

        def inbox_factory(role, engine):
            channel = InboxChannel(db_path, run_id, role, engine)
            if room:
                channel.directions = DirectionChannel(db_path, run_id, role, engine)
            return channel

        try:
            execute_run(args, store, run_id, prepared=True, folders=json.loads(row["folders"]),
                        save_stage=save_stage, defer_final=defer_final,
                        base_revision=revision,
                        delegated_questions=tuple(RULES[rule["topic"]] for rule in policy["rules"]) if policy else (),
                        technical_choice_guidance=CHOICE_INSTRUCTIONS if room and get_choices(db_path, run_id) else "",
                        inbox_factory=inbox_factory if interactive else None)
        except (ValueError, OSError, subprocess.SubprocessError, RuntimeError):
            message = {
                "QUEUED": "Git 작업 트리 또는 선택한 폴더를 준비하지 못했습니다.",
                "RUNNING": "개발 단계를 마치지 못했습니다.",
                "TESTING": "테스트 단계를 마치지 못했습니다.",
                "REVIEWING": "검토 단계를 마치지 못했습니다.",
            }.get(store.get_run(run_id).state, "작업 결과를 기록하지 못했습니다.")
            store.connection.execute("UPDATE studio_runs SET diagnostic=? WHERE id=?", (message, run_id))
            raise


def preview(db_path, run_id):
    if sys.stdin.buffer.readline() != b"preview\n":
        return
    with TaskStore(db_path, existing=True) as store:
        task = store.get_task(store.get_run(run_id).task_id)
        evidence = changes_in(Path(task.workspace))
        store.connection.execute("UPDATE studio_runs SET preview=? WHERE id=?", (json.dumps(evidence), run_id))


if __name__ == "__main__":
    action = preview if sys.argv[1] == "preview" else worker
    action(Path(sys.argv[2]), sys.argv[3])
