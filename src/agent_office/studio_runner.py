"""Persistent, locally owned Studio executions using the existing task journal."""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import threading
from types import SimpleNamespace
import uuid

from .engines import DEFAULT_DEVELOPER, DEFAULT_REVIEWER
from .events import snapshot_request
from .models import ACTIVE, OfficeError, OwnerInfo, TaskSpec, validate_id
from .ownership import RunGuard, RunBusyError, recover_run
from .store import TaskStore
from .studio_diff import changes_in
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
    r.outcome,r.task_id,t.workspace,t.branch,r.attempts,r.started_at AS created_at,
    t.updated_at,s.allow_edits"""
SOURCE = """ FROM studio_runs s JOIN runs r ON r.run_id=s.id JOIN tasks t ON t.task_id=r.task_id"""
DETAIL = ",s.folders,s.developer_output,s.test_output,s.review_output,s.changes,s.diff,s.diagnostic"


def finish_drained(store, run_id, *, owner_lost, preview_finished):
    """Publish the result after all workers exit. Stop and finish race in SQLite."""
    with store._write():
        if store.get_run(run_id).state not in ACTIVE:
            return
        row = store.connection.execute(
            "SELECT stop_requested, proposed_state, proposed_outcome, preview FROM studio_runs WHERE id=?", (run_id,)
        ).fetchone()
        if row["stop_requested"]:
            state, outcome = "CANCELED", "CANCELED"
        elif row["proposed_state"] and not owner_lost:
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
        self.stale = set()
        self.closed = False
        self.guard = RunGuard.studio(self.path)
        self.guard.__enter__()
        try:
            with TaskStore(self.path) as store:
                store.connection.execute(SCHEMA)
                stale = store.connection.execute(
                    "SELECT r.run_id FROM runs r JOIN studio_runs s ON s.id=r.run_id "
                    "WHERE r.host_name=? AND r.ended_at IS NULL", (platform.node(),)
                ).fetchall()
                for row in stale:
                    if not self._recover(store, row[0]):
                        self.stale.add(row[0])
        except BaseException:
            self.guard.__exit__(None, None, None)
            raise

    @staticmethod
    def _recover(store, run_id):
        try:
            if store.get_run(run_id).state not in ACTIVE:
                return True
            run = recover_run(store, run_id, host_name=platform.node())
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
        for run_id, child in list(self.children.items()):
            if child.poll() is not None:
                child.stdin.close()
                del self.children[run_id]
                self._recover(store, run_id)

    def _previous(self, store, run_id, digest):
        row = store.connection.execute("SELECT request_hash FROM studio_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return None
        if row[0] != digest:
            raise OfficeError("REQUEST_CONFLICT")
        return self._detail(store, run_id)

    def submit(self, catalog, run_id, workspace_id, indices, task, allow_edits):
        # A registered folder's index is stable even if its checkout was moved
        # after the original request succeeded and its HTTP response was lost.
        request = json.dumps([workspace_id, indices, task, allow_edits], ensure_ascii=False)
        digest = hashlib.sha256(request.encode()).hexdigest()
        with self.lock, TaskStore(self.path, existing=True) as store:
            existing = self._previous(store, run_id, digest)
            if existing:
                return existing
        repo, folders = catalog.select(workspace_id, indices)
        return self.start(run_id, workspace_id, repo, folders, task, allow_edits, request_digest=digest)

    def start(self, run_id: str, workspace_id: str, repo: Path, folders: list[str],
              task: str, allow_edits: bool, *, request_digest=None):
        validate_id(run_id)
        if not isinstance(task, str) or not task.strip() or len(task.encode("utf-8")) > 8192:
            raise ValueError("Task must be between 1 and 8192 UTF-8 bytes")
        if type(allow_edits) is not bool or not folders or not all(isinstance(f, str) for f in folders):
            raise ValueError("Invalid Studio request")
        repo = Path(repo).resolve()
        folder_json = json.dumps(folders, ensure_ascii=False)
        request = json.dumps([workspace_id, str(repo), folders, task, allow_edits], ensure_ascii=False)
        digest = request_digest or hashlib.sha256(request.encode()).hexdigest()
        with self.lock, TaskStore(self.path, existing=True) as store:
            if self.closed:
                raise OfficeError("RUN_BUSY")
            self._refresh(store)
            existing = self._previous(store, run_id, digest)
            if existing:
                return existing
            map_folders(repo, folders)
            spec = TaskSpec(uuid.uuid4().hex, str(repo), task, DEFAULT_DEVELOPER, DEFAULT_REVIEWER, 2)

            def register(connection):
                connection.execute(
                    "INSERT INTO studio_runs(id,workspace_id,folders,allow_edits,request_hash) VALUES (?,?,?,?,?)",
                    (run_id, workspace_id, folder_json, allow_edits, digest),
                )

            store.create_run(spec, run_id, plan_worktree(repo, run_id),
                             OwnerInfo(platform.node(), uuid.uuid4().hex), register=register)
            try:
                child = subprocess.Popen(
                    [sys.executable, "-m", "agent_office.studio_process", str(self.path), run_id],
                    cwd=Path(__file__).resolve().parents[1], stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=os.name != "nt",
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                self.children[run_id] = child
                child.stdin.write(json.dumps(task, ensure_ascii=False).encode("utf-8") + b"\n")
                child.stdin.flush()
            except OSError:
                if run_id in self.children:
                    self.children[run_id].stdin.close()
                self._recover(store, run_id)
            return self._detail(store, run_id)

    def _detail(self, store, run_id):
        row = store.connection.execute(SUMMARY + DETAIL + SOURCE + " WHERE s.id=?", (run_id,)).fetchone()
        if row is None:
            raise OfficeError("NOT_FOUND")
        result = dict(row)
        result["folders"] = json.loads(result["folders"])
        result["allow_edits"] = bool(result["allow_edits"])
        return result

    def detail(self, run_id):
        validate_id(run_id)
        with self.lock, TaskStore(self.path, existing=True) as store:
            self._refresh(store)
            return self._detail(store, run_id)

    def runs(self):
        with self.lock, TaskStore(self.path, existing=True) as store:
            self._refresh(store)
            rows = store.connection.execute(SUMMARY + SOURCE + " ORDER BY r.started_at DESC LIMIT 200")
            return [dict(row, allow_edits=bool(row["allow_edits"])) for row in rows]

    def stop(self, run_id):
        validate_id(run_id)
        with self.lock, TaskStore(self.path, existing=True) as store:
            self._refresh(store)
            result = self._detail(store, run_id)
            if result["state"] in ACTIVE or result["state"] == "STOP_REQUESTED":
                run = store.get_run(run_id)
                if run.host_name != platform.node():
                    raise OfficeError("HOST_MISMATCH")
                child = self.children.get(run_id)
                if child is None:
                    self._recover(store, run_id)
                else:
                    store.connection.execute(
                        "UPDATE studio_runs SET stop_requested=1 WHERE id=? AND EXISTS "
                        "(SELECT 1 FROM runs WHERE run_id=? AND ended_at IS NULL)", (run_id, run_id),
                    )
                    child.stdin.close()
            return self._detail(store, run_id)

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            # Register all pipe closes before touching SQLite. Even if a stop
            # marker cannot be saved, EOF still makes every supervisor drain.
            with ExitStack() as cleanup:
                cleanup.callback(self.guard.__exit__, None, None, None)
                for child in self.children.values():
                    cleanup.callback(child.wait, timeout=30)
                for child in self.children.values():
                    cleanup.callback(child.stdin.close)
                with TaskStore(self.path, existing=True) as store, store._write():
                    store.connection.executemany(
                        "UPDATE studio_runs SET stop_requested=1 WHERE id=? AND EXISTS "
                        "(SELECT 1 FROM runs WHERE run_id=? AND ended_at IS NULL)",
                        [(run_id, run_id) for run_id in self.children],
                    )
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

        try:
            execute_run(args, store, run_id, prepared=True, folders=json.loads(row["folders"]),
                        save_stage=save_stage, defer_final=defer_final)
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
