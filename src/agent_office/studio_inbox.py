"""Studio-owned human requests. Only the originating live channel may send a decision."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
from pathlib import Path
import platform
import uuid

from .events import snapshot_request
from .models import OfficeError, validate_id
from .ownership import RunBusyError, RunGuard
from .store import TaskStore, now


# Both lists fit the remote Studio response budget even with JSON escaping.
LIST_LIMIT = 100
RUN_LIMIT = 100
POLL_SECONDS = 0.15
ROLE_STATE = {"developer": "RUNNING", "reviewer": "REVIEWING"}
PUBLIC = ("id", "run_id", "kind", "status", "role", "engine", "attempt", "prompt", "decision",
          "answer", "created_at", "decided_at", "delivered_at", "expired_at")
SCHEMA = """CREATE TABLE IF NOT EXISTS studio_requests (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES studio_runs(id),
    role TEXT NOT NULL, engine TEXT NOT NULL, attempt INTEGER NOT NULL,
    holder TEXT NOT NULL, session_hash TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('question','permission')), prompt TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','decided','delivered','expired')),
    decision TEXT, answer TEXT, created_at TEXT NOT NULL, decided_at TEXT,
    delivered_at TEXT, expired_at TEXT)"""
RUN = """SELECT r.state,r.attempts,r.ended_at,s.stop_requested,s.proposed_state,
    t.developer_engine,t.reviewer_engine,t.current_run_id=r.run_id AS current_run,
    EXISTS(SELECT 1 FROM studio_interactive_runs i WHERE i.id=s.id) AS interactive
    FROM studio_runs s JOIN runs r ON r.run_id=s.id
    JOIN tasks t ON t.task_id=r.task_id
    WHERE r.run_id=? AND r.host_name=?"""


def _text(value):
    if (not isinstance(value, str) or not value.strip() or len(value) > 8192
            or len(value.encode("utf-8")) > 8192 or snapshot_request(value) != value):
        raise ValueError("요청과 답변에는 8192 UTF-8 바이트 이내의 민감 정보 없는 원문이 필요합니다.")
    return value


def _action(payload):
    if not isinstance(payload, dict):
        raise ValueError("응답 내용을 확인하세요.")
    action = payload.get("action")
    if action == "answer" and set(payload) == {"action", "answer"}:
        return action, _text(payload["answer"])
    if action in ("approve", "reject") and set(payload) == {"action"}:
        return action, None
    raise ValueError("질문에는 답변을, 권한 요청에는 승인 또는 거절을 선택하세요.")


def _public(row):
    return {key: row[key] for key in PUBLIC}


def _run(db, run_id):
    row = db.execute(RUN, (validate_id(run_id), platform.node())).fetchone()
    if row is None:
        raise OfficeError("NOT_FOUND")
    return row


def _active(run, role, engine):
    return (run["interactive"] and run["current_run"] and not run["stop_requested"]
            and run["ended_at"] is None and run["proposed_state"] is None
            and role in ROLE_STATE and run["state"] == ROLE_STATE[role]
            and engine == run[role + "_engine"])


def _request(db, run_id, request_id):
    row = db.execute("SELECT * FROM studio_requests WHERE run_id=? AND id=?",
                     (run_id, validate_id(request_id))).fetchone()
    if row is None:
        raise OfficeError("NOT_FOUND")
    return row


def _expire(db, condition, params):
    db.execute("UPDATE studio_requests SET status='expired',expired_at=? "
               "WHERE status IN ('pending','decided') AND " + condition, (now(), *params))


def expire_requests(connection, run_id):
    """Close all undelivered requests as part of the caller's stop/recovery transaction."""
    _expire(connection, "run_id=?", (run_id,))


def _holder_alive(path, holder):
    # The same nonblocking OS primitive used for run recovery. A killed worker
    # releases its lock even when the supervisor has not marked the run terminal.
    try:
        with RunGuard(path, holder):
            return False
    except RunBusyError:
        return True


class Inbox:
    def __init__(self, db_path):
        self.path = Path(db_path)
        with TaskStore(self.path, existing=True) as store, store._write():
            store.connection.execute(SCHEMA)
            store.connection.execute("CREATE INDEX IF NOT EXISTS studio_requests_run "
                                     "ON studio_requests(run_id)")
            store.connection.execute("CREATE INDEX IF NOT EXISTS studio_requests_queue "
                                     "ON studio_requests(status)")

    def _refresh(self, db, row, run):
        if row["status"] in ("pending", "decided") and (
            not _active(run, row["role"], row["engine"]) or row["attempt"] != run["attempts"]
            or not _holder_alive(self.path, row["holder"])
        ):
            _expire(db, "id=?", (row["id"],))
            return _request(db, row["run_id"], row["id"])
        return row

    def pending(self):
        with TaskStore(self.path, existing=True) as store, store._write():
            db = store.connection
            result = []
            # Stale rows do not consume a slot. Oldest first means a new run
            # cannot push a live older request off the list.
            while len(result) < LIST_LIMIT:
                rows = db.execute(
                    "SELECT q.* FROM studio_requests q JOIN runs r ON r.run_id=q.run_id "
                    "WHERE q.status='pending' AND r.host_name=? "
                    "ORDER BY q.rowid LIMIT ? OFFSET ?",
                    (platform.node(), LIST_LIMIT - len(result), len(result)),
                ).fetchall()
                if not rows:
                    break
                for row in rows:
                    current = self._refresh(db, row, _run(db, row["run_id"]))
                    if current["status"] == "pending":
                        result.append(_public(current))
            return result

    def history(self, run_id):
        with TaskStore(self.path, existing=True) as store, store._write():
            db = store.connection
            run = _run(db, run_id)
            rows = db.execute("SELECT * FROM studio_requests WHERE run_id=? ORDER BY rowid",
                              (run_id,)).fetchall()
            return [_public(self._refresh(db, row, run)) for row in rows]

    def decide(self, run_id, request_id, payload):
        action, answer = _action(payload)
        with TaskStore(self.path, existing=True) as store, store._write():
            db = store.connection
            run = _run(db, run_id)
            row = self._refresh(db, _request(db, run_id, request_id), run)
            if (row["kind"] == "question") != (action == "answer"):
                raise ValueError("요청 종류에 맞는 응답을 선택하세요.")
            if row["decision"] is not None:
                if row["decision"] != action or row["answer"] != answer:
                    raise OfficeError("REQUEST_CONFLICT")
                return _public(row)
            if row["status"] == "pending":
                db.execute("UPDATE studio_requests SET status='decided',decision=?,answer=?,decided_at=? WHERE id=?",
                           (action, answer, now(), request_id))
                return _public(_request(db, run_id, request_id))
        # Commit any expiry detected above before reporting the refusal.
        raise OfficeError("REQUEST_CLOSED")


class InboxChannel:
    """One instance per live CLI conversation, including when a workflow retries."""
    def __init__(self, db_path, run_id, role, engine):
        self.path = Path(db_path)
        self.run_id = validate_id(run_id)
        self.role = role
        self.engine = engine
        self.holder = uuid.uuid4().hex
        self.guard = RunGuard(self.path, self.holder)
        self.session_hash = None
        self.closed = False
        self.ready_to_send = set()
        self.awaiting_receipt = set()

    def _bind(self, session_id):
        if self.closed:
            raise OfficeError("REQUEST_CLOSED")
        if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 4096:
            raise ValueError("실행기 세션을 확인하세요.")
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        if self.session_hash is None:
            self.guard.__enter__()
            self.session_hash = digest
        elif self.session_hash != digest:
            raise OfficeError("REQUEST_CLOSED")

    async def ask(self, kind, prompt, session_id):
        if kind not in ("question", "permission"):
            raise ValueError("지원하지 않는 요청입니다.")
        prompt = _text(prompt)
        self._bind(session_id)
        with TaskStore(self.path, existing=True) as store:
            db = store.connection
            with store._write():
                run = _run(db, self.run_id)
                if not _active(run, self.role, self.engine):
                    raise OfficeError("REQUEST_CLOSED")
                if db.execute("SELECT count(*) FROM studio_requests WHERE run_id=?",
                              (self.run_id,)).fetchone()[0] >= RUN_LIMIT:
                    raise ValueError("실행당 요청 한도에 도달했습니다.")
                request_id = uuid.uuid4().hex
                db.execute("INSERT INTO studio_requests "
                           "(id,run_id,role,engine,attempt,holder,session_hash,kind,prompt,status,created_at) "
                           "VALUES (?,?,?,?,?,?,?,?,?,'pending',?)",
                           (request_id, self.run_id, self.role, self.engine, run["attempts"], self.holder,
                            self.session_hash, kind, prompt, now()))
            try:
                while True:
                    row = self._current(db, request_id)
                    if row["status"] == "decided":
                        self.ready_to_send.add(request_id)
                        return _public(row)
                    await asyncio.sleep(POLL_SECONDS)
            except BaseException:
                _expire(db, "id=? AND holder=?", (request_id, self.holder))
                raise

    def _current(self, db, request_id):
        row = _request(db, self.run_id, request_id)
        run = _run(db, self.run_id)
        if (self.closed or row["holder"] != self.holder or row["session_hash"] != self.session_hash
                or row["status"] not in ("pending", "decided") or row["attempt"] != run["attempts"]
                or not _active(run, self.role, self.engine)):
            raise OfficeError("REQUEST_CLOSED")
        return row

    @contextmanager
    def sending(self, request_id):
        """Order the single synchronous pipe write against stop. Never await inside."""
        if request_id not in self.ready_to_send:
            raise OfficeError("REQUEST_CLOSED")
        self.ready_to_send.remove(request_id)
        with TaskStore(self.path, existing=True) as store, store._write():
            self._current(store.connection, request_id)
            yield
        self.awaiting_receipt.add(request_id)

    def delivered(self, request_id):
        """Record a successful write only after the transport confirms delivery."""
        if request_id not in self.awaiting_receipt:
            raise OfficeError("REQUEST_CLOSED")
        self.awaiting_receipt.remove(request_id)
        with TaskStore(self.path, existing=True) as store, store._write():
            self._current(store.connection, request_id)
            store.connection.execute("UPDATE studio_requests SET status='delivered',delivered_at=? WHERE id=?",
                                     (now(), request_id))

    def expire(self):
        if self.closed:
            return
        self.closed = True
        if self.session_hash is not None:
            try:
                with TaskStore(self.path, existing=True) as store:
                    _expire(store.connection, "run_id=? AND holder=?", (self.run_id, self.holder))
            finally:
                self.guard.__exit__(None, None, None)
