"""SQLite journal. No transaction spans external execution."""
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

from .models import (ACTIVE, TERMINAL, STATES, OUTCOMES, TRANSITIONS, EVENT_TYPES,
                     TaskRecord, RunRecord, EventRecord, TaskSpec, OwnerInfo, OfficeError, validate_id)
from .workspace import Worktree
from .events import snapshot_request, sanitize_payload, encode_payload

SCHEMA = (
    "CREATE TABLE tasks(task_id TEXT PRIMARY KEY, project TEXT NOT NULL, request_snapshot TEXT NOT NULL, developer_engine TEXT NOT NULL, reviewer_engine TEXT NOT NULL, workspace TEXT NOT NULL, branch TEXT NOT NULL, state TEXT NOT NULL, outcome TEXT, attempts INTEGER NOT NULL, max_attempts INTEGER NOT NULL, current_run_id TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)",
    "CREATE TABLE runs(run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id), host_name TEXT NOT NULL, owner_token TEXT NOT NULL, state TEXT NOT NULL, outcome TEXT, attempts INTEGER NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, next_seq INTEGER NOT NULL)",
    "CREATE UNIQUE INDEX one_active_run ON runs(task_id) WHERE state IN ('QUEUED','RUNNING','TESTING','REVIEWING')",
    "CREATE TABLE events(run_id TEXT NOT NULL REFERENCES runs(run_id), seq INTEGER NOT NULL, operation_key TEXT NOT NULL, event_type TEXT NOT NULL, role TEXT, engine TEXT, timestamp TEXT NOT NULL, payload_json TEXT NOT NULL, operation_json TEXT NOT NULL, PRIMARY KEY(run_id,seq), UNIQUE(run_id,operation_key))",
)


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class TaskStore:
    def __init__(self, db_path: Path, *, readonly: bool = False):
        self.path = Path(db_path).expanduser().resolve()
        self.readonly = readonly
        if readonly and not self.path.is_file():
            raise OfficeError("DB_NOT_FOUND")
        if not readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path.as_uri() + ("?mode=ro" if readonly else "?mode=rwc"), uri=True, isolation_level=None, timeout=5)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA busy_timeout=5000")
            if readonly:
                self.connection.execute("PRAGMA query_only=ON")
            if readonly:
                self._check_schema()
            else:
                with self._write():
                    version = self.connection.execute("PRAGMA user_version").fetchone()[0]
                    if version == 0 and not self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchone():
                        for statement in SCHEMA:
                            self.connection.execute(statement)
                        self.connection.execute("PRAGMA user_version=1")
                    self._check_schema()
        except BaseException:
            self.close()
            raise

    def _check_schema(self):
        if self.connection.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise OfficeError("SCHEMA_UNSUPPORTED")

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def _write(self):
        if self.readonly:
            raise OfficeError("STORE_IO")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def _get(self, table, column, value, record):
        validate_id(value)
        row = self.connection.execute(f"SELECT * FROM {table} WHERE {column}=?", (value,)).fetchone()
        if row is None:
            raise OfficeError("NOT_FOUND")
        return record(**dict(row))

    def get_task(self, task_id):
        return self._get("tasks", "task_id", task_id, TaskRecord)

    def get_run(self, run_id):
        return self._get("runs", "run_id", run_id, RunRecord)

    def list_tasks(self, *, limit=50):
        self._limit(limit, 200)
        return [TaskRecord(**dict(row)) for row in self.connection.execute("SELECT * FROM tasks ORDER BY updated_at DESC, task_id ASC LIMIT ?", (limit,))]

    @staticmethod
    def _limit(value, maximum):
        if type(value) is not int or not 1 <= value <= maximum:
            raise ValueError("Invalid limit")

    @staticmethod
    def _event(row):
        return EventRecord(row["run_id"], row["seq"], row["operation_key"], row["event_type"], row["role"], row["engine"], row["timestamp"], json.loads(row["payload_json"]))

    def list_events(self, task_id, *, run_id=None, after_seq=0, limit=100):
        self._limit(limit, 501)
        if type(after_seq) is not int or after_seq < 0:
            raise ValueError("Invalid cursor")
        task = self.get_task(task_id)
        run = self.get_run(run_id or task.current_run_id)
        if run.task_id != task_id:
            raise ValueError("Run does not belong to task")
        return [self._event(row) for row in self.connection.execute("SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?", (run.run_id, after_seq, limit))]

    def create_run(self, spec: TaskSpec, run_id: str, intent: Worktree, owner: OwnerInfo):
        validate_id(spec.task_id)
        validate_id(run_id)
        if type(spec.max_attempts) is not int or spec.max_attempts < 1 or spec.developer_engine not in ("claude", "codex") or spec.reviewer_engine not in ("claude", "codex"):
            raise ValueError("Invalid task specification")
        stamp = now()
        with self._write():
            if self.connection.execute("SELECT 1 FROM tasks WHERE task_id=?", (spec.task_id,)).fetchone():
                raise ValueError("Task already exists; replay is not supported")
            task = TaskRecord(spec.task_id, spec.project, snapshot_request(spec.request_snapshot), spec.developer_engine, spec.reviewer_engine, str(intent.path), intent.branch, "QUEUED", None, 0, spec.max_attempts, run_id, stamp, stamp)
            self.connection.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(asdict(task).values()))
            self.connection.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)", (run_id, spec.task_id, owner.host_name, owner.owner_token, "QUEUED", None, 0, stamp, None, 1))
            self._record(run_id, operation_key=f"{run_id}:0:run:RUN_CREATED", event_type="RUN_CREATED", payload={"phase": "run"})
        return self.get_run(run_id)

    def record_transition(self, run_id, *, operation_key, event_type, state=None, outcome=None, attempts=None, payload=None):
        with self._write():
            return self._record(run_id, operation_key=operation_key, event_type=event_type, state=state, outcome=outcome, attempts=attempts, payload=payload)

    def _record(self, run_id, *, operation_key, event_type, state=None, outcome=None, attempts=None, payload=None):
        run = self.get_run(run_id)
        task = self.get_task(run.task_id)
        if event_type not in EVENT_TYPES or not isinstance(operation_key, str) or not re.fullmatch(r"[A-Za-z0-9:_.-]{1,128}", operation_key):
            raise ValueError("Invalid event identity")
        payload = sanitize_payload(payload if payload is not None else {})
        encode_payload(payload)
        signature = encode(dict(event_type=event_type, state=state, outcome=outcome, attempts=attempts, payload=payload))
        previous = self.connection.execute("SELECT * FROM events WHERE run_id=? AND operation_key=?", (run_id, operation_key)).fetchone()
        if previous:
            if previous["operation_json"] != signature:
                raise ValueError("Conflicting operation replay")
            return self._event(previous)
        target = state or run.state
        count = run.attempts if attempts is None else attempts
        if run.state not in ACTIVE or target not in STATES or (target != run.state and target not in TRANSITIONS[run.state] | {"FAILED", "CANCELED", "RECOVERY_REQUIRED"}):
            raise ValueError("Invalid state transition")
        if type(count) is not int or not run.attempts <= count <= task.max_attempts:
            raise ValueError("Invalid attempt budget")
        if outcome is not None and outcome not in OUTCOMES:
            raise ValueError("Invalid outcome")
        if target in ACTIVE and outcome is not None:
            raise ValueError("Active state cannot have an outcome")
        expected = {"PLAN_ONLY": {"PLAN_ONLY"}, "READY_FOR_HUMAN_REVIEW": {"APPROVED"}, "WAITING_USER": {"REVIEW_UNDECIDED"}, "CANCELED": {"CANCELED"}, "RECOVERY_REQUIRED": {"INTERRUPTED"}, "FAILED": {"DEVELOPER_FAILED", "TEST_FAILED", "REVIEW_FAILED", "CHANGES_REQUESTED"}}
        if target in TERMINAL and outcome not in expected[target]:
            raise ValueError("Outcome does not match state")
        for key, expected_value in (("state", target), ("attempt", count), ("outcome", outcome)):
            if key in payload and payload[key] != expected_value:
                raise ValueError("Metadata contradicts transition")
        stamp = now()
        payload.update(state=target, attempt=count)
        if outcome is not None:
            payload["outcome"] = outcome
        self.connection.execute("UPDATE tasks SET state=?, outcome=?, attempts=?, updated_at=? WHERE task_id=?", (target, outcome, count, stamp, run.task_id))
        self.connection.execute("UPDATE runs SET state=?, outcome=?, attempts=?, ended_at=?, next_seq=next_seq+1 WHERE run_id=?", (target, outcome, count, stamp if target in TERMINAL else None, run_id))
        self.connection.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)", (run_id, run.next_seq, operation_key, event_type, payload.get("role"), payload.get("engine"), stamp, encode_payload(payload), signature))
        return self._event(self.connection.execute("SELECT * FROM events WHERE run_id=? AND seq=?", (run_id, run.next_seq)).fetchone())
