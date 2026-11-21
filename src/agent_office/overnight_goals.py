"""Workspace verification defaults and the user-visible goal journal."""
from datetime import datetime, timedelta
import difflib
import json
from pathlib import Path
import re
import subprocess
from tempfile import TemporaryDirectory

from .events import snapshot_request
from .models import ACTIVE, OfficeError, validate_id
from .store import TaskStore, encode, now


def ensure_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS studio_goal_policies(
        workspace_id TEXT PRIMARY KEY, policy_json TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_goals(
        id TEXT PRIMARY KEY REFERENCES studio_runs(id), workspace_id TEXT NOT NULL,
        request TEXT NOT NULL, base_commit TEXT NOT NULL, scope_json TEXT NOT NULL,
        criteria_json TEXT NOT NULL, policy_json TEXT NOT NULL, created_at TEXT NOT NULL,
        adopted_attempt INTEGER, adopted_digest TEXT, adopted_at TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_goal_adoptions(
        goal_id TEXT NOT NULL REFERENCES studio_goals(id), seq INTEGER PRIMARY KEY,
        decision_id TEXT NOT NULL, attempt INTEGER NOT NULL, digest TEXT NOT NULL, previous_digest TEXT,
        at TEXT NOT NULL, UNIQUE(goal_id,decision_id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_goal_redacted_requests(
        goal_id TEXT PRIMARY KEY REFERENCES studio_goals(id))""")


def workspace_policy(db, workspace_id, test_files=None):
    from .goal_verification import default_policy

    saved = db.execute("SELECT policy_json FROM studio_goal_policies WHERE workspace_id=?", (workspace_id,)).fetchone()
    return json.loads(saved[0]) if saved else default_policy(test_files)


def save_policy(db, workspace_id, data):
    from .goal_verification import validate_policy

    policy = validate_policy(data)
    db.execute("INSERT INTO studio_goal_policies VALUES(?,?) ON CONFLICT(workspace_id) "
               "DO UPDATE SET policy_json=excluded.policy_json", (workspace_id, encode(policy)))
    return policy


def criteria_for(task, criteria):
    if criteria is None:
        return [snapshot_request(task)]
    if not isinstance(criteria, list) or not 1 <= len(criteria) <= 20 or not all(
            isinstance(text, str) and text.strip() and len(text.encode('utf-8')) <= 8192 for text in criteria):
        raise ValueError("목표의 완료 기준을 확인하세요.")
    return [snapshot_request(text.strip()) for text in criteria]


def register(db, run_id, workspace, request, revision, scope, assignment):
    saved = snapshot_request(request)
    from .goal_peer import pin_targets
    pin_targets(db, run_id, assignment["goal_policy"])
    db.execute("""INSERT INTO studio_goals
        (id,workspace_id,request,base_commit,scope_json,criteria_json,policy_json,created_at)
        VALUES(?,?,?,?,?,?,?,?)""", (run_id, workspace, saved, revision, encode(scope),
                                      encode(assignment["goal_criteria"]), encode(assignment["goal_policy"]), now()))
    if request != saved:
        db.execute("INSERT INTO studio_goal_redacted_requests VALUES(?)", (run_id,))


def goal_route(method, route):
    comparison = re.fullmatch(r"/runs/([a-f0-9]{32})/goal/compare/([1-9][0-9]{0,6})/([1-9][0-9]{0,6})", route)
    if method == "GET" and comparison:
        return comparison[1], "compare", (int(comparison[2]), int(comparison[3]))
    match = re.fullmatch(r"/runs/([a-f0-9]{32})/goal(?:/(adopt|recover|candidates/([1-9][0-9]{0,6})))?", route)
    if match:
        goal, action, attempt = match.groups()
        if method == "GET" and (action is None or attempt) or method == "POST" and action in {"adopt", "recover"}:
            return goal, "candidate" if attempt else action, int(attempt) if attempt else None
    return None


def timeline(db, run_id):
    events = []

    def add(at, kind, stage, attempt, detail):
        if at:
            events.append(dict(at=at, kind=kind, stage=stage, attempt=attempt, detail=detail))

    for row in db.execute("SELECT timestamp,event_type,role,payload_json,operation_json FROM events "
                          "WHERE run_id=? ORDER BY seq", (run_id,)):
        operation = json.loads(row["operation_json"])
        add(row["timestamp"], row["event_type"], row["role"], operation.get("attempts"), row["payload_json"])
    for row in db.execute("SELECT * FROM studio_directions WHERE run_id=?", (run_id,)):
        add(row["created_at"], "direction", row["role"], row["attempt"], row["message"] + " · " + row["delivery"])
    for row in db.execute("SELECT * FROM studio_requests WHERE run_id=?", (run_id,)):
        add(row["created_at"], row["kind"], row["role"], row["attempt"], row["prompt"])
        add(row["decided_at"], "decision", row["role"], row["attempt"], row["answer"] or row["decision"])
        add(row["delivered_at"], "delivered", row["role"], row["attempt"], row["decision"])
    for row in db.execute("SELECT revision,reason,created_at,approved_at,approved_by FROM studio_meeting_revisions "
                          "WHERE run_id=?", (run_id,)):
        add(row["created_at"], "plan", "meeting", None, row["reason"] or str(row["revision"]))
        add(row["approved_at"], "plan_approved", "meeting", None, row["approved_by"])
    for row in db.execute("SELECT * FROM studio_goal_adoptions WHERE goal_id=? ORDER BY seq", (run_id,)):
        add(row["at"], "adopted", "operator", row["attempt"], row["digest"])
    return events


def report(path, run_id):
    from . import goal_recovery, goal_verification

    with TaskStore(path, readonly=True) as store:
        db = store.connection
        db.execute("BEGIN")
        validate_id(run_id)
        row = db.execute("SELECT * FROM studio_goals WHERE id=?", (run_id,)).fetchone()
        if row is None:
            # Historical team parents have no individual worker run in the runs table.
            if not db.execute("SELECT 1 FROM studio_runs WHERE id=? UNION ALL "
                              "SELECT 1 FROM studio_teams WHERE id=?", (run_id, run_id)).fetchone():
                raise OfficeError("NOT_FOUND")
            return None
        run = store.get_run(run_id)
        verification = goal_verification.describe(db, run_id) or dict(checks=[], candidates=[], best_attempt=None, stop_reason="")
        recovery = goal_recovery.detail(store, run_id)
        events = timeline(db, run_id)
        checks = verification.get("checks", [])
        for check in checks:
            start = check["started_at"]
            events.append(dict(at=start, kind="verification_started", stage=check["stage"],
                               attempt=check["attempt"], detail=check["host"]))
            if check["status"] != "running":
                finished = check.get("finished_at") or (
                    datetime.fromisoformat(start) + timedelta(seconds=check["elapsed_seconds"])).isoformat()
                events.append(dict(at=finished, kind="verification", stage=check["stage"],
                                   attempt=check["attempt"], detail=check["status"]))
        candidates = verification.get("candidates", [])
        for candidate in candidates:
            if candidate.get("created_at"):
                events.append(dict(at=candidate["created_at"], kind="candidate", stage="develop",
                                   attempt=candidate["attempt"], detail=candidate["digest"]))
            if candidate.get("review_at"):
                events.append(dict(at=candidate["review_at"], kind="review", stage="review",
                                   attempt=candidate["attempt"], detail=candidate.get("review_status")))
        events.sort(key=lambda item: datetime.fromisoformat(item["at"]))
        last = events[-1]["at"] if events else row["created_at"]
        observed = now()
        age = max(0, (datetime.fromisoformat(observed) - datetime.fromisoformat(last)).total_seconds())
        criteria = json.loads(row["criteria_json"])
        meeting_checks = db.execute("""SELECT criterion,status FROM studio_meeting_checks WHERE run_id=?
            AND revision=(SELECT MAX(revision) FROM studio_meeting_revisions WHERE run_id=?)""",
                                    (run_id, run_id)).fetchall()
        verified = {check["criterion"] for check in meeting_checks if check["status"] == "passed"}
        manual = list(dict.fromkeys([criterion for criterion in criteria if criterion not in verified] +
                                    [check["criterion"] for check in meeting_checks if check["status"] != "passed"]))
        next_action = recovery.get("reason")
        if row["adopted_attempt"] is not None:
            next_action = "채택한 후보의 남은 수동 확인 항목을 검토하세요." if manual else "채택 이력을 확인하세요."
        elif run.state not in ACTIVE and run.state != "RECOVERY_REQUIRED":
            next_action = verification.get("stop_reason") or "종료된 실행의 결과와 검증 범위를 확인하세요."
        external = None
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='studio_external_origins'").fetchone():
            from .github_office import public_origin
            external = public_origin(db, run_id)
        return dict(verification, id=external["goal_id"] if external else run_id, external=external,
                    run_id=run_id, workspace_id=row["workspace_id"],
                    request=row["request"], base_commit=row["base_commit"], scope=json.loads(row["scope_json"]),
                    criteria=criteria, manual_checks=manual, policy=json.loads(row["policy_json"]),
                    state=run.state, outcome=run.outcome, created_at=row["created_at"], last_progress_at=last, observed_at=observed,
                    progress_uncertain=run.state in ACTIVE and age > 120, events=events, recovery=recovery,
                    active_seconds=recovery.get("active_seconds"), waiting_seconds=recovery.get("user_wait_seconds"),
                    next_action=next_action, adopted_attempt=row["adopted_attempt"],
                    adopted_digest=row["adopted_digest"], adopted_at=row["adopted_at"],
                    merge_status="not_recorded", deployment_status="not_recorded")


def candidate(path, run_id, attempt):
    from .goal_verification import candidate_diff

    with TaskStore(path, readonly=True) as store:
        db = store.connection
        if not db.execute("SELECT 1 FROM studio_goals WHERE id=?", (run_id,)).fetchone():
            raise OfficeError("NOT_FOUND")
        return dict(run_id=run_id, attempt=attempt, diff=candidate_diff(db, run_id, attempt))


def compare(path, run_id, attempts):
    from .release_store import run_git

    with TaskStore(path, readonly=True) as store:
        db = store.connection
        source = db.execute("""SELECT t.project,g.base_commit FROM studio_goals g JOIN runs r ON r.run_id=g.id
            JOIN tasks t ON t.task_id=r.task_id WHERE g.id=?""", (run_id,)).fetchone()
        if not source or not db.execute("SELECT 1 FROM sqlite_master WHERE name='studio_goal_candidates'").fetchone():
            raise OfficeError("NOT_FOUND")
        patches = []
        for attempt in attempts:
            row = db.execute("SELECT patch FROM studio_goal_candidates WHERE run_id=? AND attempt=?",
                             (run_id, attempt)).fetchone()
            if not row:
                raise OfficeError("NOT_FOUND")
            patches.append(row[0])
    try:
        project = Path(source["project"])
        objects = run_git(project, "rev-parse", "--path-format=absolute", "--git-path", "objects").decode().strip()
        with TemporaryDirectory(prefix="office-goal-compare-") as temp:
            isolated = Path(temp)
            args = ["--object-format=sha256"] if len(source["base_commit"]) == 64 else []
            run_git(isolated, "init", "--bare", "-q", *args)
            env = {"GIT_ALTERNATE_OBJECT_DIRECTORIES": objects}
            trees = []
            for patch in patches:
                run_git(isolated, "read-tree", source["base_commit"], env=env)
                if patch:
                    run_git(isolated, "apply", "--cached", "--binary", "--whitespace=nowarn", "-",
                            env=env, data=patch.encode())
                trees.append(run_git(isolated, "write-tree", env=env).decode().strip())
            delta = run_git(isolated, "diff", "--no-ext-diff", "--no-textconv", "--no-renames",
                            *trees, env=env).decode("utf-8", "replace")
        form = "files"
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        # Evidence remains comparable even if the operator moved the original checkout.
        delta = "".join(difflib.unified_diff(patches[0].splitlines(True), patches[1].splitlines(True),
                                            f"attempt-{attempts[0]}.patch", f"attempt-{attempts[1]}.patch"))
        form = "patches"
    return dict(run_id=run_id, from_attempt=attempts[0], to_attempt=attempts[1],
                diff=snapshot_request(delta), format=form, truncated=len(delta.encode()) > 8192)


def adopt(path, run_id, data):
    from .goal_verification import describe

    if (set(data) != {"id", "attempt", "digest", "previous_digest"} or type(data["attempt"]) is not int or
            not isinstance(data["digest"], str) or not re.fullmatch("[a-f0-9]{64}", data["digest"])):
        raise ValueError("검토한 후보와 저장된 변경 식별자가 필요합니다.")
    receipt = {"run_id": run_id, "id": validate_id(data["id"]), "attempt": data["attempt"], "digest": data["digest"]}
    with TaskStore(path, existing=True) as store, store._write():
        db = store.connection
        goal = db.execute("SELECT * FROM studio_goals WHERE id=?", (run_id,)).fetchone()
        if not goal:
            raise OfficeError("NOT_FOUND")
        prior = db.execute("SELECT attempt,digest,previous_digest FROM studio_goal_adoptions "
                           "WHERE goal_id=? AND decision_id=?", (run_id, data["id"])).fetchone()
        if prior:
            if tuple(prior) != (data["attempt"], data["digest"], data["previous_digest"]):
                raise OfficeError("REQUEST_CONFLICT")
            return receipt
        if data["previous_digest"] != goal["adopted_digest"]:
            raise OfficeError("REQUEST_CONFLICT")
        if store.get_run(run_id).state in ACTIVE:
            raise OfficeError("RUN_BUSY")
        candidates = (describe(db, run_id) or {}).get("candidates", [])
        if not any(c["eligible"] and c["attempt"] == data["attempt"] and c["digest"] == data["digest"] and
                   c["status"] == "passed" and c["review_status"] == "approved" for c in candidates):
            raise OfficeError("REQUEST_CONFLICT")
        at = now()
        db.execute("UPDATE studio_goals SET adopted_attempt=?,adopted_digest=?,adopted_at=? WHERE id=?",
                   (data["attempt"], data["digest"], at, run_id))
        db.execute("INSERT INTO studio_goal_adoptions(attempt,digest,at,goal_id,decision_id,previous_digest) "
                   "VALUES(?,?,?,?,?,?)", (data["attempt"], data["digest"], at, run_id, data["id"], data["previous_digest"]))
    return receipt
