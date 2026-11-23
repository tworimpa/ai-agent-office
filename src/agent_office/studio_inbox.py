"""Studio-owned human requests. Only the originating live channel may send a decision."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import platform
import uuid

from . import away_ledger
from .dialogue import DelegationWithdrawn, PRODUCT_DECISION, REQUEST_KINDS
from .events import snapshot_request
from .models import OfficeError, validate_id
from .ownership import RunGuard
from .role_skills import role_guidance
from .store import TaskStore, now
from .technical_choices import CHOICE_TOPIC, public_prompt, valid_receipt


# Both lists fit the remote Studio response budget even with JSON escaping.
LIST_LIMIT = 100
RUN_LIMIT = 100
POLL_SECONDS = 0.15
DEFERRED_REASONS = {
    "evidence_unavailable": "기준 커밋에서 답변을 뒷받침할 근거를 확인하지 못했습니다.",
    "evidence_ambiguous": "근거 문서에서 하나의 답을 정할 수 없습니다.",
    "worktree_changed": "작업 트리가 위임 기준 커밋과 일치하지 않습니다.",
    "verifier_failed": "독립 검증자가 답과 근거를 확인하지 못했습니다.",
    "specialist_failed": "전문가가 근거 있는 답변을 제출하지 못했습니다.",
    "out_of_scope": away_ledger.OUT_OF_SCOPE,
}
ROLE_STATE = {"developer": "RUNNING", "reviewer": "REVIEWING"}
PUBLIC = ("id", "run_id", "kind", "status", "role", "engine", "attempt", "prompt", "decision",
          "answer", "created_at", "decided_at", "delivered_at", "expired_at")
DECISION_TYPE = (
    f"decision_type TEXT CHECK(decision_type IS NULL OR (kind='question' AND decision_type='{PRODUCT_DECISION}'))"
)
SCHEMA = """CREATE TABLE IF NOT EXISTS studio_requests (
    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES studio_runs(id),
    role TEXT NOT NULL, engine TEXT NOT NULL, attempt INTEGER NOT NULL,
    holder TEXT NOT NULL, session_hash TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('question','permission')), prompt TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','decided','delivered','expired')),
    decision TEXT, answer TEXT, created_at TEXT NOT NULL, decided_at TEXT,
    delivered_at TEXT, expired_at TEXT, """ + DECISION_TYPE + ")"
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
    raise ValueError("질문과 제품 결정에는 답변을, 권한 요청에는 승인 또는 거절을 선택하세요.")


def _public(db, row):
    result = {key: row[key] for key in PUBLIC}
    result["kind"] = row["decision_type"] or row["kind"]
    if result["kind"] == "question":
        result["prompt"] = public_prompt(result["prompt"])
    check = db.execute("SELECT * FROM studio_away_checks WHERE request_id=?", (row["id"],)).fetchone()
    result["source"] = "operator" if row["decision"] is not None else None
    if row["decision_type"] == PRODUCT_DECISION and row["decision"] == "answer":
        from .workspace_coordination import _exists
        if _exists(db):
            shared = db.execute("SELECT evidence FROM workspace_decision_uses WHERE target_request=?", (row["id"],)).fetchone()
            if shared:
                result["shared_decision"] = json.loads(shared[0])
    result["delegation_reason"] = check["reason"] if check else None
    result["delegation_in_progress"] = bool(check and row["status"] == "pending"
                                            and check["reason"] is None and check["evidence"] is None)
    if check and check["evidence"] is not None:
        result["source"] = "delegate"
        result["delegation_details"] = {key: check[key] for key in ("topic", "specialist_engine", "verifier_engine")}
        result["delegation_details"]["evidence"] = json.loads(check["evidence"])
    return result


def _run(db, run_id):
    row = db.execute(RUN, (validate_id(run_id), platform.node())).fetchone()
    if row is None:
        raise OfficeError("NOT_FOUND")
    from .goal_usage import exists
    if exists(db):
        row = dict(row)
        # The immutable task assignment records the original engine. An active
        # fallback session owns only its own role and attempt while its call is open.
        for role, engine in db.execute("SELECT role,engine FROM studio_usage_roles WHERE run_id=? AND attempt=?",
                                       (run_id,row["attempts"])):
            row[role + "_engine"] = engine
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


def holder_alive(path, holder):
    # A killed worker releases its lock even before the run becomes terminal.
    return RunGuard(path, holder).is_held()


def _refresh(db, path, row, run):
    if row["status"] in ("pending", "decided") and (
        not _active(run, row["role"], row["engine"]) or row["attempt"] != run["attempts"]
        or not holder_alive(path, row["holder"])
    ):
        _expire(db, "id=?", (row["id"],))
        return _request(db, row["run_id"], row["id"])
    return row


def refresh_status(db, path, run_id, request_id):
    return _refresh(db, path, _request(db, run_id, request_id), _run(db, run_id))["status"]


def _live_pending(db, path, limit=None, workspace_id=None):
    # Oldest first; expired rows do not consume a slot. A rowid cursor stays
    # stable as _refresh expires stale rows inside this transaction.
    last = 0
    yielded = 0
    scope, arguments = ("", ()) if workspace_id is None else (" AND s.workspace_id=?", (validate_id(workspace_id),))
    while limit is None or yielded < limit:
        rows = db.execute("""SELECT q.rowid,q.* FROM studio_requests q JOIN runs r ON r.run_id=q.run_id
            JOIN studio_runs s ON s.id=q.run_id WHERE q.status='pending' AND r.host_name=? AND q.rowid>?"""
            + scope + " ORDER BY q.rowid LIMIT ?",
            (platform.node(), last, *arguments, LIST_LIMIT if limit is None else limit - yielded)).fetchall()
        if not rows:
            return
        for row in rows:
            last = row["rowid"]
            if _refresh(db, path, row, _run(db, row["run_id"]))["status"] == "pending":
                yielded += 1
                yield row


def live_pending_counts(db, path):
    counts = {"questions": 0, "permissions": 0}
    for row in _live_pending(db, path):
        key = (row["decision_type"] or row["kind"]) + "s"
        counts[key] = counts.get(key, 0) + 1
    return counts


class Inbox:
    def __init__(self, db_path):
        self.path = Path(db_path)
        with TaskStore(self.path, existing=True) as store, store._write():
            store.connection.execute(SCHEMA)
            # Preserve request IDs and their away ledger foreign keys in old journals.
            if "decision_type" not in {row["name"] for row in store.connection.execute(
                    "PRAGMA table_info(studio_requests)")}:
                store.connection.execute("ALTER TABLE studio_requests ADD COLUMN " + DECISION_TYPE)
            away_ledger.ensure_schema(store.connection)
            store.connection.execute("CREATE INDEX IF NOT EXISTS studio_requests_run "
                                     "ON studio_requests(run_id)")
            store.connection.execute("CREATE INDEX IF NOT EXISTS studio_requests_queue "
                                     "ON studio_requests(status)")

    def pending(self, *, workspace_id=None):
        with TaskStore(self.path, existing=True) as store, store._write():
            return [_public(store.connection, row)
                    for row in _live_pending(store.connection, self.path, LIST_LIMIT, workspace_id)]

    def history(self, run_id):
        with TaskStore(self.path, existing=True) as store, store._write():
            db = store.connection
            run = _run(db, run_id)
            rows = db.execute("SELECT * FROM studio_requests WHERE run_id=? ORDER BY rowid",
                              (run_id,)).fetchall()
            return [_public(db, _refresh(db, self.path, row, run)) for row in rows]

    @staticmethod
    def settled_history(db, run_id):
        rows = db.execute("SELECT * FROM studio_requests WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
        if any(row["status"] in ("pending", "decided") for row in rows):
            return None  # The holder may have exited; reconcile through history().
        return [_public(db, row) for row in rows]

    def decide(self, run_id, request_id, payload):
        action, answer = _action(payload)
        with TaskStore(self.path, existing=True) as store, store._write():
            db = store.connection
            run = _run(db, run_id)
            row = _refresh(db, self.path, _request(db, run_id, request_id), run)
            if (row["kind"] == "question") != (action == "answer"):
                raise ValueError("요청 종류에 맞는 응답을 선택하세요.")
            if row["decision"] is not None:
                if row["decision"] != action or row["answer"] != answer:
                    raise OfficeError("REQUEST_CONFLICT")
                return _public(db, row)
            if row["status"] == "pending":
                db.execute("UPDATE studio_requests SET status='decided',decision=?,answer=?,decided_at=? WHERE id=?",
                           (action, answer, now(), request_id))
                return _public(db, _request(db, run_id, request_id))
        # Commit any expiry detected above before reporting the refusal.
        raise OfficeError("REQUEST_CLOSED")


class InboxChannel:
    """One instance per live CLI conversation, including when a workflow retries."""
    def __init__(self, db_path, run_id, role, engine, *, goal_task_id=None):
        self.path = Path(db_path)
        self.run_id = validate_id(run_id)
        self.role = role
        self.engine = engine
        self.holder = uuid.uuid4().hex
        self.goal_task_id = goal_task_id
        if goal_task_id is not None:
            with TaskStore(self.path, existing=True) as store:
                store.connection.execute("INSERT INTO studio_goal_workers VALUES (?,?,?,?,NULL)",
                                         (self.holder, self.run_id, goal_task_id, now()))
        self.guard = RunGuard(self.path, self.holder)
        self.session_hash = None
        self.closed = False
        # None means a human decision was already recorded. A tuple is a
        # private proposal, eligible only at the immediately guarded pipe write.
        self.ready_to_send = {}
        self.awaiting_operator = set()
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
        if kind not in REQUEST_KINDS:
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
                           "(id,run_id,role,engine,attempt,holder,session_hash,kind,decision_type,prompt,status,created_at) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?)",
                           (request_id, self.run_id, self.role, self.engine, run["attempts"], self.holder,
                            self.session_hash, "question" if kind == PRODUCT_DECISION else kind,
                            PRODUCT_DECISION if kind == PRODUCT_DECISION else None, prompt, now()))
                plan = (away_ledger.begin_question(db, _request(db, self.run_id, request_id))
                        if kind == "question" else None)
                from .workspace_coordination import reuse_product_answer
                shared = kind == PRODUCT_DECISION and reuse_product_answer(db,self.run_id,run["attempts"],request_id,prompt)
                if plan is None and not shared:
                    from .goal_recovery import note_wait
                    note_wait(db, request_id)
            consultation = (asyncio.create_task(self._consult(store, request_id, plan)) if plan else None)
            try:
                while True:
                    row = self._current(db, request_id)
                    if row["status"] == "decided":
                        self.ready_to_send[request_id] = None
                        return _public(db, row)
                    if consultation:
                        if consultation.done():
                            proposal = await consultation
                            consultation = None
                            if proposal is not None:
                                current, reason = away_ledger.match_question(db, row)
                                if current == plan:
                                    answer, evidence = proposal
                                    self.ready_to_send[request_id] = (plan, answer, evidence)
                                    # This is private input for the transport to encode.
                                    # The journal still shows a pending human-answerable question.
                                    return dict(_public(db, row), source="delegate", decision="answer",
                                                answer=answer)
                                away_ledger.defer(db, request_id, reason or away_ledger.CHANGED)
                        else:
                            current, reason = away_ledger.match_question(db, row)
                            if current != plan:
                                away_ledger.defer(db, request_id, reason or away_ledger.CHANGED)
                                consultation.cancel()
                                await asyncio.gather(consultation, return_exceptions=True)
                                consultation = None
                    await asyncio.sleep(POLL_SECONDS)
            except BaseException:
                _expire(db, "id=? AND holder=?", (request_id, self.holder))
                raise
            finally:
                if consultation:
                    consultation.cancel()
                    await asyncio.gather(consultation, return_exceptions=True)

    async def _consult(self, store, request_id, plan):
        from .away_consult import run_consultation
        from .away_evidence import DelegationDeferred

        db = store.connection
        current, changed_reason = away_ledger.match_question(db, self._current(db, request_id))
        if current != plan:
            if changed_reason:
                away_ledger.defer(db, request_id, changed_reason)
            return

        def on_stage(role, status):
            engine = plan.specialist if role == "specialist" else plan.verifier
            away_ledger.record_role_stage(db, request_id, role, engine, status)

        try:
            guidance = role_guidance(db, self.run_id, ("specialist", "verifier"))
            result = await run_consultation(plan.repo, plan.revision, plan.rule,
                                            plan.specialist, plan.verifier, json.loads(plan.folders_json),
                                            skill_guidance=guidance, on_stage=on_stage)
            answer = _text(result["answer"])
            evidence = result["evidence"]
            if (evidence["revision"] != plan.revision or type(evidence["line"]) is not int or evidence["line"] < 1):
                raise ValueError("Invalid delegation evidence")
            receipt = {"path": evidence["path"], "revision": plan.revision, "line": evidence["line"],
                       "quote": _text(evidence["quote"])}
            if plan.topic == CHOICE_TOPIC:
                receipt.update(purpose=evidence["purpose"], option=evidence["option"])
                if not valid_receipt(public_prompt(plan.prompt), answer, receipt):
                    raise ValueError("Invalid choice evidence")
            elif receipt["path"] != plan.path:
                raise ValueError("Invalid delegation evidence path")
            elif evidence.get("method") == "committed_fact":
                receipt["method"] = "committed_fact"
            evidence = receipt
        except DelegationDeferred as exc:
            reason = DEFERRED_REASONS.get(exc.reason, away_ledger.FAILED)
        except Exception:
            # An unavailable or malformed provider result must leave the human
            # question usable. Never expose raw CLI output in the decision journal.
            reason = away_ledger.FAILED
        else:
            reason = None
        with store._write():
            row = self._current(db, request_id)
            if row["status"] != "pending":
                return
            current, changed_reason = away_ledger.match_question(db, row)
            if current != plan:
                reason = changed_reason or away_ledger.CHANGED
            if reason:
                away_ledger.defer(db, request_id, reason)
            else:
                return answer, evidence

    async def await_operator(self, request_id):
        """Resume waiting on the same live question after its private proposal was withdrawn."""
        if request_id not in self.awaiting_operator:
            raise OfficeError("REQUEST_CLOSED")
        self.awaiting_operator.remove(request_id)
        with TaskStore(self.path, existing=True) as store:
            db = store.connection
            try:
                while True:
                    row = self._current(db, request_id)
                    if row["kind"] != "question" or row["decision_type"] is not None:
                        raise OfficeError("REQUEST_CLOSED")
                    if row["status"] == "decided":
                        if row["decision"] != "answer":
                            raise OfficeError("REQUEST_CLOSED")
                        self.ready_to_send[request_id] = None
                        return _public(db, row)
                    await asyncio.sleep(POLL_SECONDS)
            except BaseException:
                _expire(db, "id=? AND holder=?", (request_id, self.holder))
                raise

    def _current(self, db, request_id):
        row = _request(db, self.run_id, request_id)
        run = _run(db, self.run_id)
        if (self.closed or self.guard.fd is None or row["holder"] != self.holder
                or row["session_hash"] != self.session_hash
                or row["role"] != self.role or row["engine"] != self.engine
                or row["status"] not in ("pending", "decided") or row["attempt"] != run["attempts"]
                or not _active(run, self.role, self.engine)):
            raise OfficeError("REQUEST_CLOSED")
        return row

    @contextmanager
    def sending(self, request_id):
        """Order one transport write with human decisions, revocation and run stop."""
        if request_id not in self.ready_to_send:
            raise OfficeError("REQUEST_CLOSED")
        proposal = self.ready_to_send.pop(request_id)
        withdrawn = False
        send_error = None
        with TaskStore(self.path, existing=True) as store, store._write():
            db = store.connection
            row = self._current(db, request_id)
            if proposal is not None:
                if row["kind"] != "question" or row["decision_type"] is not None:
                    raise OfficeError("REQUEST_CLOSED")
                plan, answer, evidence = proposal
                current, reason = away_ledger.match_question(db, row)
                if current != plan:
                    withdrawn = True
                    if row["status"] == "pending":
                        away_ledger.defer(db, request_id, reason or away_ledger.CHANGED)
                else:
                    away_ledger.record_answer(db, request_id, plan, answer, evidence)
            elif row["status"] != "decided":
                raise OfficeError("REQUEST_CLOSED")
            if not withdrawn:
                try:
                    yield
                except BaseException as exc:
                    # The transport might already have accepted bytes. Preserve
                    # the decision, expire it, and do not authorize a replay.
                    _expire(db, "id=? AND holder=?", (request_id, self.holder))
                    send_error = exc
        if withdrawn:
            self.awaiting_operator.add(request_id)
            raise DelegationWithdrawn()
        if send_error is not None:
            raise send_error
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
        try:
            if self.session_hash is not None:
                try:
                    with TaskStore(self.path, existing=True) as store:
                        _expire(store.connection, "run_id=? AND holder=?", (self.run_id, self.holder))
                finally:
                    self.guard.__exit__(None, None, None)
        finally:
            if self.goal_task_id is not None:
                with TaskStore(self.path, existing=True) as store:
                    store.connection.execute("UPDATE studio_goal_workers SET ended_at=? WHERE holder=?",
                                             (now(), self.holder))
            directions = getattr(self, "directions", None)
            if directions is not None:
                directions.close()
