"""Revisioned Workspace meeting records. Mutations share the caller's BEGIN IMMEDIATE."""
import json

from .engines import SUPPORTED_ENGINES
from .events import snapshot_request
from .meeting import validate_plan, validate_plan_scope
from .models import ACTIVE, MAX_SQLITE_INT, OfficeError, validate_id
from .store import encode, now


def ensure_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS studio_meetings(
        run_id TEXT PRIMARY KEY REFERENCES studio_runs(id), review_plan INTEGER NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('planning','awaiting_review','approved','stopped','failed')),
        revision INTEGER NOT NULL DEFAULT 0, error TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_meeting_contributions(
        run_id TEXT NOT NULL REFERENCES studio_meetings(run_id), revision INTEGER NOT NULL,
        role TEXT NOT NULL, engine TEXT NOT NULL, text TEXT NOT NULL, created_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_meeting_revisions(
        run_id TEXT NOT NULL REFERENCES studio_meetings(run_id), revision INTEGER NOT NULL,
        plan TEXT NOT NULL, source TEXT NOT NULL, reason TEXT NOT NULL, direction_id TEXT,
        answers TEXT NOT NULL, approved_by TEXT, created_at TEXT NOT NULL, approved_at TEXT,
        PRIMARY KEY(run_id,revision), UNIQUE(run_id,direction_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_meeting_decisions(
        run_id TEXT NOT NULL REFERENCES studio_meetings(run_id), id TEXT NOT NULL, revision INTEGER NOT NULL,
        payload TEXT NOT NULL, PRIMARY KEY(run_id,id), UNIQUE(run_id,revision))""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_meeting_checks(
        run_id TEXT NOT NULL, revision INTEGER NOT NULL, criterion TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('not_verified','passed','failed')), evidence TEXT NOT NULL,
        PRIMARY KEY(run_id,revision,criterion),
        FOREIGN KEY(run_id,revision) REFERENCES studio_meeting_revisions(run_id,revision))""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_meeting_work(
        run_id TEXT NOT NULL, revision INTEGER NOT NULL, task_id TEXT NOT NULL,
        engine TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL,
        FOREIGN KEY(run_id,revision) REFERENCES studio_meeting_revisions(run_id,revision))""")


def register(db, run_id, review_plan=False):
    if type(review_plan) is not bool:
        raise ValueError("계획 확인 설정을 확인하세요.")
    db.execute("INSERT OR IGNORE INTO studio_meetings(run_id,review_plan,status) VALUES (?,?,'planning')",
               (validate_id(run_id), review_plan))
    if _meeting(db, run_id)["review_plan"] != review_plan:
        raise OfficeError("REQUEST_CONFLICT")


def _meeting(db, run_id):
    row = db.execute("SELECT * FROM studio_meetings WHERE run_id=?", (validate_id(run_id),)).fetchone()
    if row is None:
        raise OfficeError("NOT_FOUND")
    return row


def _require_open(db, row, status):
    live = db.execute("""SELECT r.state FROM runs r JOIN studio_runs s ON s.id=r.run_id
        JOIN tasks t ON t.current_run_id=r.run_id AND t.task_id=r.task_id
        WHERE r.run_id=? AND r.ended_at IS NULL AND NOT s.stop_requested AND s.proposed_state IS NULL""",
                      (row["run_id"],)).fetchone()
    if row["status"] != status or live is None or live["state"] not in ACTIVE:
        raise OfficeError("REQUEST_CLOSED")


def _revision(value):
    if type(value) is not int or not 1 <= value <= MAX_SQLITE_INT:
        raise ValueError("계획 버전을 확인하세요.")
    return value


def _text(value, limit):
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or "\0" in value or snapshot_request(value) != value):
        raise ValueError("회의 내용의 길이와 민감 정보 포함 여부를 확인하세요.")
    return value


def _choices(plan):
    return [dict(item, id=f"q{index}") for index, item in enumerate(plan["questions"], 1)]


def _get_revision(db, run_id, revision):
    return db.execute("SELECT * FROM studio_meeting_revisions WHERE run_id=? AND revision=?",
                      (run_id, revision)).fetchone()


def _contribution(role, engine, text):
    if role not in ("developer", "reviewer", "coordinator") or engine not in SUPPORTED_ENGINES:
        raise ValueError("회의 담당자와 엔진을 확인하세요.")
    _text(text, 1000)
    return role, engine, text


def contribute(db, run_id, role, engine, text):
    _require_open(db, _meeting(db, run_id), "planning")
    row = _contribution(role, engine, text)
    db.execute("INSERT INTO studio_meeting_contributions VALUES (?,1,?,?,?,?)", (run_id, *row, now()))


def _validate_scope(db, run_id, plan):
    scope = db.execute("SELECT folders FROM studio_runs WHERE id=?", (run_id,)).fetchone()
    validate_plan_scope(plan, json.loads(scope["folders"]))


def _add_revision(db, meeting, plan, source, reason, direction_id, answers, approved_by):
    revision = meeting["revision"] + 1
    run_id, timestamp = meeting["run_id"], now()
    db.execute("""INSERT INTO studio_meeting_revisions
        (run_id,revision,plan,source,reason,direction_id,answers,approved_by,created_at,approved_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)""", (run_id, revision, encode(plan), source, reason, direction_id,
        encode(answers), approved_by, timestamp, timestamp if approved_by else None))
    db.execute("UPDATE studio_meetings SET revision=?,status=? WHERE run_id=?",
               (revision, "approved" if approved_by else "awaiting_review", run_id))
    db.executemany("INSERT INTO studio_meeting_checks VALUES (?,?,?,'not_verified','검증 실행 전입니다.')",
                   ((run_id, revision, item["criterion"]) for item in plan["criteria"]))


def propose(db, run_id, plan):
    meeting = _meeting(db, run_id)
    _require_open(db, meeting, "planning")
    validate_plan(plan)
    _validate_scope(db, run_id, plan)
    approved_by = None if meeting["review_plan"] or plan["questions"] else "coordinator"
    _add_revision(db, meeting, plan, "coordinator", "회의 결과를 반영한 첫 계획", None, {}, approved_by)
    return describe(db, run_id)


def decide(db, run_id, payload):
    """Consolidated operator product choices. This receipt carries no tool permissions."""
    if not isinstance(payload, dict) or set(payload) != {"id", "revision", "plan", "answers"}:
        raise ValueError("계획 버전, 편집한 계획과 제품 선택 답변을 확인하세요.")
    decision_id = validate_id(payload["id"])
    revision = _revision(payload["revision"])
    plan = validate_plan(payload["plan"])
    answers = payload["answers"]
    if (not isinstance(answers, dict) or set(answers) != {item["id"] for item in _choices(plan)}
            or any(answers[item["id"]] not in item["options"] for item in _choices(plan))):
        raise ValueError("각 제품 질문에 제시된 선택지 중 하나를 선택하세요.")
    meeting = _meeting(db, run_id)
    serial = encode(payload)
    previous = db.execute("SELECT payload FROM studio_meeting_decisions WHERE run_id=? AND id=?",
                          (run_id, decision_id)).fetchone()
    if previous:
        if previous["payload"] != serial:
            raise OfficeError("REQUEST_CONFLICT")
        return dict(describe(db, run_id), decision={"id": decision_id, "revision": revision})
    if revision != meeting["revision"]:
        raise OfficeError("REQUEST_CONFLICT")
    _require_open(db, meeting, "awaiting_review")
    _validate_scope(db, run_id, plan)
    original = json.loads(_get_revision(db, run_id, revision)["plan"])
    if plan["questions"] != original["questions"] or plan["evidence"] != original["evidence"]:
        raise ValueError("제품 질문과 조사 근거는 검토 중인 계획 버전과 일치해야 합니다.")
    if original != plan:
        _add_revision(db, meeting, plan, "operator", "운영자가 계획을 수정하고 승인했습니다.", None, answers, "operator")
    else:
        db.execute("""UPDATE studio_meeting_revisions SET answers=?,approved_by='operator',approved_at=?
            WHERE run_id=? AND revision=?""", (encode(answers), now(), run_id, revision))
        db.execute("UPDATE studio_meetings SET status='approved' WHERE run_id=?", (run_id,))
    db.execute("INSERT INTO studio_meeting_decisions VALUES (?,?,?,?)", (run_id, decision_id, revision, serial))
    return describe(db, run_id)


def revise(db, run_id, revision, plan, reason, direction_id, contributions=()):
    """Apply a direction once; new criteria always require fresh verification."""
    meeting = _meeting(db, run_id)
    _revision(revision)
    validate_id(direction_id)
    validate_plan(plan)
    _validate_scope(db, run_id, plan)
    _text(reason, 4096)
    opinions = [_contribution(item["role"], item["engine"], item["text"]) for item in contributions]
    previous = db.execute("SELECT * FROM studio_meeting_revisions WHERE run_id=? AND direction_id=?",
                          (run_id, direction_id)).fetchone()
    if previous:
        prior_opinions = db.execute("""SELECT role,engine,text FROM studio_meeting_contributions
            WHERE run_id=? AND revision=? ORDER BY rowid""", (run_id, previous["revision"]))
        if (previous["revision"] != revision + 1 or previous["plan"] != encode(plan) or previous["reason"] != reason
                or [tuple(row) for row in prior_opinions] != opinions):
            raise OfficeError("REQUEST_CONFLICT")
        return describe(db, run_id)
    if meeting["revision"] != revision:
        raise OfficeError("REQUEST_CONFLICT")
    _require_open(db, meeting, "approved")
    original = _get_revision(db, run_id, revision)
    prior_plan, prior_answers = json.loads(original["plan"]), json.loads(original["answers"])
    if plan["evidence"]["revision"] != prior_plan["evidence"]["revision"]:
        raise ValueError("추가 지시를 반영할 때 원래 기준 커밋을 보존하세요.")
    known = {(item["question"], tuple(item["options"])): prior_answers[item["id"]]
             for item in _choices(prior_plan) if item["id"] in prior_answers}
    answers = {}
    for item in _choices(plan):
        key = item["question"], tuple(item["options"])
        if key in known:
            answers[item["id"]] = known[key]
    pending = meeting["review_plan"] or len(answers) != len(plan["questions"])
    _add_revision(db, meeting, plan, "direction", reason, direction_id, answers, None if pending else "coordinator")
    db.executemany("INSERT INTO studio_meeting_contributions VALUES (?,?,?,?,?,?)",
                   ((run_id, revision + 1, *row, now()) for row in opinions))
    return describe(db, run_id)


def verify(db, run_id, revision, criterion, status, evidence):
    meeting = _meeting(db, run_id)
    if meeting["revision"] != _revision(revision):
        raise OfficeError("REQUEST_CONFLICT")
    _require_open(db, meeting, "approved")
    if status not in ("passed", "failed", "not_verified"):
        raise ValueError("완료 기준의 검증 결과를 확인하세요.")
    if status != "not_verified" or evidence != "":
        if isinstance(evidence, str):
            evidence = snapshot_request(evidence)[:4096]
        _text(evidence, 4096)
    result = db.execute("UPDATE studio_meeting_checks SET status=?,evidence=? WHERE run_id=? AND revision=? AND criterion=?",
                        (status, evidence, run_id, revision, _text(criterion, 1000)))
    if not result.rowcount:
        raise ValueError("현재 계획에 없는 완료 기준입니다.")


def stop(db, run_id):
    db.execute("""UPDATE studio_meetings SET status='stopped'
        WHERE run_id=? AND status NOT IN ('stopped','failed')""", (validate_id(run_id),))


def fail(db, run_id, reason):
    reason = snapshot_request(reason)[:1000]
    db.execute("""UPDATE studio_meetings SET status='failed',error=?
        WHERE run_id=? AND status NOT IN ('stopped','failed')""", (_text(reason, 1000), validate_id(run_id)))


def summary(db, run_id):
    row = db.execute("SELECT status,revision,error FROM studio_meetings WHERE run_id=?", (validate_id(run_id),)).fetchone()
    return dict(row) if row else None


def record_work(db, run_id, revision, task_id, engine, result):
    meeting = _meeting(db, run_id)
    _require_open(db, meeting, "approved")
    if meeting["revision"] != revision or engine not in SUPPORTED_ENGINES:
        raise OfficeError("REQUEST_CONFLICT")
    db.execute("INSERT INTO studio_meeting_work VALUES (?,?,?,?,?,?)",
               (run_id, revision, _text(task_id, 32), engine, snapshot_request(result)[:1000], now()))


def describe(db, run_id):
    """Read the plan and audit trail in one consistent SQLite snapshot."""
    if db.in_transaction:
        return _describe(db, run_id)
    db.execute("BEGIN")
    try:
        return _describe(db, run_id)
    finally:
        db.rollback()


def _describe(db, run_id):
    meeting = db.execute("SELECT * FROM studio_meetings WHERE run_id=?", (validate_id(run_id),)).fetchone()
    if meeting is None:
        return None
    contributions = [dict(row) for row in db.execute("""SELECT revision,role,engine,text,created_at
        FROM studio_meeting_contributions WHERE run_id=? ORDER BY rowid""", (run_id,))]
    checks = {}
    for row in db.execute("""SELECT revision,criterion,status,evidence FROM studio_meeting_checks
            WHERE run_id=? ORDER BY rowid""", (run_id,)):
        checks.setdefault(row["revision"], []).append({key: row[key] for key in ("criterion", "status", "evidence")})
    revisions = []
    for row in db.execute("""SELECT revision,plan,source,reason,direction_id,answers,approved_by,created_at,approved_at
            FROM studio_meeting_revisions WHERE run_id=? ORDER BY revision""", (run_id,)):
        revisions.append(dict(row, plan=json.loads(row["plan"]), answers=json.loads(row["answers"]),
                              verification=checks[row["revision"]]))
    current = revisions[-1] if revisions else None
    decision = db.execute("""SELECT id,revision FROM studio_meeting_decisions
        WHERE run_id=? ORDER BY revision DESC LIMIT 1""", (run_id,)).fetchone()
    work = [dict(row) for row in db.execute("""SELECT revision,task_id,engine,result,created_at
        FROM studio_meeting_work WHERE run_id=? ORDER BY rowid""", (run_id,))]
    return dict(meeting, review_plan=bool(meeting["review_plan"]), contributions=contributions, revisions=revisions, work=work,
                plan=current["plan"] if current else None, choices=_choices(current["plan"]) if current else [],
                answers=current["answers"] if current else {}, verification=current["verification"] if current else [],
                decision=dict(decision) if decision else None)
