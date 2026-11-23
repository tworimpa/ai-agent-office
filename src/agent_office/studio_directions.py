"""Operator directions scoped to one active Studio role and workflow attempt."""
from contextlib import contextmanager
import json
import time
from pathlib import Path

from .events import snapshot_request
from .models import MAX_SQLITE_INT, OfficeError, validate_id
from .ownership import RunGuard
from .store import TaskStore, now
from .studio_inbox import ROLE_STATE, _active, _run


MESSAGE_BYTES = 4096
RUN_LIMIT = 20
PUBLIC = ("id", "role", "attempt", "message", "created_at", "delivery")
SCHEMA = (
    """CREATE TABLE IF NOT EXISTS studio_directions (
        run_id TEXT NOT NULL REFERENCES studio_runs(id), id TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('developer','reviewer')), attempt INTEGER NOT NULL,
        message TEXT NOT NULL, created_at TEXT NOT NULL, delivery TEXT NOT NULL
            CHECK(delivery IN ('queued','sending','delivered','uncertain','undelivered')),
        PRIMARY KEY(run_id,id))""",
    """CREATE TABLE IF NOT EXISTS studio_closed_direction_phases (
        run_id TEXT NOT NULL REFERENCES studio_runs(id), role TEXT NOT NULL, attempt INTEGER NOT NULL,
        PRIMARY KEY(run_id,role,attempt))""",
)


def _payload(payload):
    if not isinstance(payload, dict) or set(payload) != {"id", "role", "attempt", "message"}:
        raise ValueError("추가 지시에는 ID, 담당 역할, 작업 시도와 메시지가 필요합니다.")
    direction_id = validate_id(payload["id"])
    role, message = payload["role"], payload["message"]
    attempt = payload["attempt"]
    if role not in ("developer", "reviewer"):
        raise ValueError("개발자 또는 리뷰어를 선택하세요.")
    if type(attempt) is not int or not 1 <= attempt <= MAX_SQLITE_INT:
        raise ValueError("추가 지시의 대상 시도를 확인하세요.")
    if (not isinstance(message, str) or not message.strip() or len(message) > MESSAGE_BYTES
            or len(message.encode("utf-8")) > MESSAGE_BYTES or "\x00" in message
            or snapshot_request(message) != message):
        raise ValueError("추가 지시는 민감 정보 없이 UTF-8 4096바이트 이내로 입력하세요.")
    return direction_id, role, attempt, message


def _public(row):
    return {key: row[key] for key in PUBLIC}


def _phase_open(db, path, run_id, run, role, attempt):
    return (_active(run, role, run[role + "_engine"]) and run["attempts"] == attempt
            and db.execute("""SELECT 1 FROM studio_room_assignments a WHERE a.run_id=? AND NOT EXISTS (
                SELECT 1 FROM studio_closed_direction_phases c WHERE c.run_id=a.run_id AND c.role=? AND c.attempt=?)""",
                (run_id, role, attempt)).fetchone()
            and RunGuard(path, run_id).is_held())


def _settle(db, run_id, condition, values):
    db.execute("UPDATE studio_directions SET delivery=CASE delivery WHEN 'sending' THEN 'uncertain' "
               "ELSE 'undelivered' END WHERE run_id=? AND delivery IN ('queued','sending') AND " + condition,
               (run_id, *values))


def _close(db, run_id, role, attempt):
    db.execute("INSERT OR IGNORE INTO studio_closed_direction_phases VALUES (?,?,?)", (run_id, role, attempt))
    _settle(db, run_id, "role=? AND attempt=?", (role, attempt))


class Directions:
    def __init__(self, db_path):
        self.path = Path(db_path)
        with TaskStore(self.path, existing=True) as store, store._write():
            for statement in SCHEMA:
                store.connection.execute(statement)

    def _refresh(self, db, run_id):
        run = _run(db, run_id)
        role = next((role for role, state in ROLE_STATE.items() if run["state"] == state), None)
        if role and _phase_open(db, self.path, run_id, run, role, run["attempts"]):
            _settle(db, run_id, "(role!=? OR attempt!=?)", (role, run["attempts"]))
        elif not db.execute("""SELECT 1 FROM studio_goal_stage_waits w JOIN studio_usage_holds h
                ON h.run_id=w.run_id WHERE w.run_id=? AND w.ended_at IS NULL AND h.kind='quota_wait'""",
                (run_id,)).fetchone():
            _settle(db, run_id, "1", ())
        return run

    def history(self, run_id):
        with TaskStore(self.path, existing=True) as store, store._write():
            db = store.connection
            self._refresh(db, run_id)
            rows = db.execute("SELECT * FROM studio_directions WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
            return [_public(row) for row in rows]

    @staticmethod
    def settled_history(db, run_id):
        rows = db.execute("SELECT * FROM studio_directions WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
        if any(row["delivery"] in ("queued", "sending") for row in rows):
            return None
        return [_public(row) for row in rows]

    def submit(self, run_id, payload):
        direction_id, role, attempt, message = _payload(payload)
        with TaskStore(self.path, existing=True) as store, store._write():
            db = store.connection
            run = self._refresh(db, run_id)
            row = db.execute("SELECT * FROM studio_directions WHERE run_id=? AND id=?", (run_id, direction_id)).fetchone()
            if row is not None:
                if row["role"] != role or row["message"] != message or row["attempt"] != attempt:
                    raise OfficeError("REQUEST_CONFLICT")
                return _public(row)
            if _phase_open(db, self.path, run_id, run, role, attempt):
                if db.execute("SELECT count(*) FROM studio_directions WHERE run_id=?", (run_id,)).fetchone()[0] >= RUN_LIMIT:
                    raise ValueError("한 실행에 추가 지시를 최대 20건 보낼 수 있습니다.")
                created_at = now()
                db.execute("INSERT INTO studio_directions VALUES (?,?,?,?,?,?,'queued')",
                           (run_id, direction_id, role, attempt, message, created_at))
                return {"id": direction_id, "role": role, "attempt": attempt, "message": message,
                        "created_at": created_at, "delivery": "queued"}
        raise OfficeError("REQUEST_CLOSED")


class DirectionChannel:
    """One channel follows one live adapter session, including its continuation turns."""

    def __init__(self, db_path, run_id, role, engine):
        self.path, self.run_id, self.role = Path(db_path), validate_id(run_id), role
        self.closed = False
        self.replan = None
        with TaskStore(self.path, existing=True) as store:
            db = store.connection
            run = _run(db, run_id)
            self.attempt = run["attempts"]
            if not _active(run, role, engine) or not self._open(db, run):
                raise OfficeError("REQUEST_CLOSED")

    def _open(self, db, run):
        return not self.closed and _phase_open(db, self.path, self.run_id, run, self.role, self.attempt)

    def next(self, *, available=True):
        """Claim queued rows after a verified provider turn, or close the role to further submissions."""
        with TaskStore(self.path, existing=True) as store, store._write():
            db = store.connection
            run = _run(db, self.run_id)
            if not self._open(db, run):
                raise OfficeError("REQUEST_CLOSED")
            phase = (self.run_id, self.role, self.attempt)
            rows = db.execute("SELECT * FROM studio_directions WHERE run_id=? AND role=? AND attempt=? "
                              "AND delivery IN ('queued','sending') ORDER BY rowid", phase).fetchall()
            if any(row["delivery"] == "sending" for row in rows):
                raise OfficeError("REQUEST_CLOSED")
            if not rows or not available:
                _close(db, *phase)
            else:
                # Persist before writing any bytes or launching a provider. A crash
                # across the transport boundary can never move these rows back to queued.
                db.execute("UPDATE studio_directions SET delivery='sending' WHERE run_id=? AND role=? AND attempt=? "
                           "AND delivery='queued'", phase)
        if not rows or not available:
            self.closed = True
            return None
        delivery = _Delivery(self, rows)
        if self.replan:
            started = time.monotonic()
            delivery.prompt += "\n\nUpdated meeting plan and decisions:\n" + self.replan(rows)
            delivery.meeting_seconds = time.monotonic() - started
        return delivery

    def close(self):
        if not self.closed:
            with TaskStore(self.path, existing=True) as store, store._write():
                _close(store.connection, self.run_id, self.role, self.attempt)
            self.closed = True


class _Delivery:
    def __init__(self, channel, rows):
        self.channel = channel
        self.ids = [(row["run_id"], row["id"]) for row in rows]
        self.started = False
        self.meeting_seconds = 0.0
        self.prompt = (
            "Continue the assigned work in this same session. The operator added the directions listed below in order. "
            "Apply them within the original role, worktree, task scope and permission limits. "
            "This message supplies no tool approval or permission grant. Keep all original output requirements."
            "\n\nOperator directions (JSON array):\n"
            + json.dumps([row["message"] for row in rows], ensure_ascii=False)
        )

    @contextmanager
    def sending(self):
        """Serialize the initial transport write with Studio stop and human requests."""
        channel = self.channel
        with TaskStore(channel.path, existing=True) as store, store._write():
            if self.started:
                raise OfficeError("REQUEST_CLOSED")
            db = store.connection
            run = _run(db, channel.run_id)
            pending = db.execute("SELECT 1 FROM studio_requests WHERE run_id=? AND role=? AND attempt=? "
                                 "AND status IN ('pending','decided')", (channel.run_id, channel.role, channel.attempt)).fetchone()
            if channel._open(db, run) and pending is None:
                self.started = True
                yield
                return
            db.executemany("UPDATE studio_directions SET delivery='undelivered' WHERE run_id=? AND id=? "
                           "AND delivery IN ('sending','uncertain')", self.ids)
        raise OfficeError("REQUEST_CLOSED")

    def delivered(self):
        if not self.started:
            raise OfficeError("REQUEST_CLOSED")
        with TaskStore(self.channel.path, existing=True) as store, store._write():
            result = store.connection.executemany("UPDATE studio_directions SET delivery='delivered' WHERE run_id=? AND id=? "
                                                  "AND delivery='sending'", self.ids)
            if result.rowcount != len(self.ids):
                raise OfficeError("REQUEST_CLOSED")
