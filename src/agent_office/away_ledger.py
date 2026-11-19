"""Run-scoped Studio delegation policy, question checks, and return report."""
from dataclasses import dataclass
import json
from pathlib import Path
import platform

from .away_expert import RULES, in_scope
from .models import OfficeError, validate_id
from .store import TaskStore, encode, now


# Five 8 KiB strings across a decision and an active-run row, each
# potentially escaped sixfold, leave headroom in the 12 MiB SSH frame.
REPORT_LIMIT = 40
NO_RULE = "사전 위임한 질문과 정확히 일치하지 않습니다."
OUT_OF_SCOPE = "근거 파일이 선택한 작업 폴더 범위를 벗어납니다."
REVOKED = "운영자가 이 실행의 위임을 종료했습니다."
CHANGED = "검토 중 위임 범위가 변경되었습니다."
FAILED = "전문가와 검증자가 근거 있는 답변을 확정하지 못했습니다."


def ensure_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS studio_away_policies(
        run_id TEXT PRIMARY KEY REFERENCES studio_runs(id), policy_json TEXT NOT NULL,
        revision TEXT NOT NULL, revoked_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_away_checks(
        request_id TEXT PRIMARY KEY REFERENCES studio_requests(id), reason TEXT,
        topic TEXT, specialist_engine TEXT, verifier_engine TEXT, evidence TEXT)""")


def register_policy(db, run_id, policy, revision):
    if policy is not None:
        db.execute("INSERT INTO studio_away_policies(run_id,policy_json,revision) VALUES (?,?,?)",
                   (run_id, encode(policy), revision))


def _policy_row(db, run_id):
    row = db.execute("""SELECT p.policy_json,p.revision,p.revoked_at,t.workspace,s.folders
        FROM studio_runs s JOIN runs r ON r.run_id=s.id JOIN tasks t ON t.task_id=r.task_id
        LEFT JOIN studio_away_policies p ON p.run_id=s.id
        WHERE s.id=? AND r.host_name=?""", (validate_id(run_id), platform.node())).fetchone()
    if row is None:
        raise OfficeError("NOT_FOUND")
    return row


def get_revision(db_path, run_id):
    with TaskStore(db_path, readonly=True) as store:
        return _policy_row(store.connection, run_id)["revision"]


def get_policy(db_path, run_id):
    with TaskStore(db_path, readonly=True) as store:
        row = _policy_row(store.connection, run_id)
        return json.loads(row["policy_json"]) if row["policy_json"] and not row["revoked_at"] else None


def revoke_policy(db_path, run_id):
    with TaskStore(db_path, existing=True) as store, store._write():
        db = store.connection
        _policy_row(db, run_id)
        db.execute("UPDATE studio_away_policies SET revoked_at=? WHERE run_id=? AND revoked_at IS NULL",
                   (now(), run_id))
        db.execute("""UPDATE studio_away_checks SET reason=?
            WHERE reason IS NULL AND evidence IS NULL AND request_id IN
            (SELECT id FROM studio_requests WHERE run_id=? AND status='pending')""", (REVOKED, run_id))
    return {"run_id": run_id, "active": False}


@dataclass(frozen=True)
class Consultation:
    repo: Path
    revision: str
    topic: str
    path: str
    specialist: str
    verifier: str
    policy_json: str
    folders_json: str

    @property
    def rule(self):
        return {"topic": self.topic, "path": self.path}


def match_question(db, request):
    """Used at question creation and again under the final decision write lock."""
    if request["kind"] != "question" or request["status"] != "pending":
        return None, None
    row = _policy_row(db, request["run_id"])
    if row["policy_json"] is None:
        return None, None
    if row["revoked_at"]:
        return None, REVOKED
    policy = json.loads(row["policy_json"])
    for rule in policy["rules"]:
        if request["prompt"] == RULES.get(rule["topic"]):
            if not in_scope(rule["path"], json.loads(row["folders"])):
                return None, OUT_OF_SCOPE
            return Consultation(Path(row["workspace"]), row["revision"], rule["topic"], rule["path"],
                                policy["specialist_engine"], policy["verifier_engine"], row["policy_json"],
                                row["folders"]), None
    return None, NO_RULE


def begin_question(db, request):
    consultation, reason = match_question(db, request)
    if consultation or reason:
        db.execute("INSERT INTO studio_away_checks(request_id,reason) VALUES (?,?)", (request["id"], reason))
    return consultation


def defer(db, request_id, reason):
    db.execute("UPDATE studio_away_checks SET reason=? WHERE request_id=? AND reason IS NULL AND evidence IS NULL",
               (reason, request_id))


def record_answer(db, request_id, consultation, answer, evidence):
    # Caller holds BEGIN IMMEDIATE, with a fresh match for the original channel.
    updated = db.execute("""UPDATE studio_requests SET status='decided',decision='answer',answer=?,decided_at=?
        WHERE id=? AND status='pending' AND kind='question'""", (answer, now(), request_id))
    if updated.rowcount != 1:
        raise OfficeError("REQUEST_CLOSED")
    updated = db.execute("""UPDATE studio_away_checks SET topic=?,specialist_engine=?,verifier_engine=?,evidence=?
        WHERE request_id=? AND reason IS NULL AND evidence IS NULL""",
        (consultation.topic, consultation.specialist, consultation.verifier, encode(evidence), request_id))
    if updated.rowcount != 1:
        raise OfficeError("REQUEST_CLOSED")


def return_report(db_path):
    # Local import keeps the ledger independent of the worker's inbox lifecycle.
    from .studio_inbox import live_pending_counts, refresh_status

    with TaskStore(db_path, existing=True) as store, store._write():
        db = store.connection
        host = platform.node()
        counts = live_pending_counts(db, store.path)
        joined = """ FROM studio_away_checks c JOIN studio_requests q ON q.id=c.request_id
            JOIN runs r ON r.run_id=q.run_id JOIN tasks t ON t.task_id=r.task_id
            WHERE c.evidence IS NOT NULL AND r.host_name=?"""
        counts["delegated"] = db.execute("SELECT count(*)" + joined, (host,)).fetchone()[0]
        decisions = db.execute("""SELECT q.id,q.run_id,t.request_snapshot AS task,q.prompt,q.answer,
            q.status,q.decided_at,c.topic,c.specialist_engine,c.verifier_engine,c.evidence,
            r.state AS run_state,r.outcome""" + joined + " ORDER BY q.decided_at DESC,q.rowid DESC LIMIT ?",
            (host, REPORT_LIMIT))
        delegated = [dict(row, evidence=json.loads(row["evidence"])) for row in decisions]
        for receipt in delegated:
            if receipt["status"] == "decided":
                receipt["status"] = refresh_status(db, store.path, receipt["run_id"], receipt["id"])
        active_join = """ FROM studio_away_policies p JOIN studio_runs s ON s.id=p.run_id
            JOIN runs r ON r.run_id=s.id JOIN tasks t ON t.task_id=r.task_id
            WHERE p.revoked_at IS NULL AND r.host_name=? AND r.ended_at IS NULL AND s.stop_requested=0
            AND s.proposed_state IS NULL AND t.current_run_id=r.run_id"""
        counts["active"] = db.execute("SELECT count(*)" + active_join, (host,)).fetchone()[0]
        active = db.execute("SELECT r.run_id,t.request_snapshot AS task" + active_join +
                            " ORDER BY r.started_at DESC,r.run_id DESC LIMIT ?", (host, REPORT_LIMIT))
        return {"counts": counts, "delegated": delegated, "active": [dict(row) for row in active],
                "limit": REPORT_LIMIT}
