"""Describe evidence for a Workspace seat without grading the difficulty of its jobs."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
import platform
from statistics import median


AXES = (("analysis", "분석"), ("implementation", "구현"), ("verification", "검증"),
        ("review", "리뷰"), ("project", "프로젝트 이해"), ("collaboration", "협업·인계"))
SPECIALTIES = {"coordinator": "범위·배정·결정 조율", "developer": "원인 분석·구현·재현", "reviewer": "독립 리뷰·검증 근거 확인"}
WINDOW_DAYS = 90
RUN_LIMIT = 120


def _facts(db, ids):
    result = defaultdict(lambda: defaultdict(list))
    if not ids:
        return result
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    params = ",".join("?" for _ in ids)
    queries = (
        ("checks", "studio_goal_checks", "attempt,position,receipt", ""),
        ("candidates", "studio_goal_candidates", "attempt,record", ""),
        ("reviews", "studio_goal_reviews", "attempt,status,digest", "AND kind='review'"),
        ("directions", "studio_directions", "id,role,attempt", "AND delivery='delivered'"),
        ("revisions", "studio_meeting_revisions", "revision,direction_id", "AND approved_at IS NOT NULL"),
        ("contributions", "studio_meeting_contributions", "revision,role", ""),
        ("criteria", "studio_meeting_checks", "revision,criterion", "AND status='passed'"),
        ("answers", "studio_requests", "id,role,attempt", "AND status='delivered' AND decision='answer'"),
    )
    for key, table, fields, clause in queries:
        if table in tables:
            for row in db.execute(f"SELECT run_id,{fields} FROM {table} WHERE run_id IN ({params}) {clause}", ids):
                result[row["run_id"]][key].append(row)
    return result


def _evidence(row, facts, role):
    """One strongest trace per axis and run; never assign pytest or human actions to an agent."""
    rid = row["run_id"]
    found = {}

    def record(axis, level, label, section, attempt=None, **details):
        if axis not in found or found[axis]["strength"] < level:
            found[axis] = dict(run_id=rid, task=row["task"][:64], strength=level, label=label,
                               section=section, attempt=attempt, **details)

    candidates = {r["attempt"]: json.loads(r["record"]) for r in facts["candidates"]}
    approved = {r["attempt"] for r in facts["reviews"] if r["status"] == "approved"
                and candidates.get(r["attempt"], {}).get("digest") == r["digest"]}
    adopted = row["adopted_attempt"]
    changed = [r for r in facts["reviews"] if r["status"] == "changes_requested"]
    for check in facts["checks"] if role == "developer" else ():
        receipt = json.loads(check["receipt"])
        failed = receipt.get("failed_ids") or []
        if check["attempt"] == 0 and receipt.get("status") == "failed" and failed:
            record("analysis", 4, "원본에서 실패 재현 · " + failed[0][:100], "checks", 0, position=check["position"])
        fixed = receipt.get("fixed_ids") or []
        if check["attempt"] > 0 and fixed and receipt.get("status") == "passed":
            record("verification", 5 if check["attempt"] in approved else 4,
                   "수정 후보에서 실패 검사 해결 · " + fixed[0][:100], "checks", check["attempt"], position=check["position"])
    for attempt, candidate in candidates.items():
        if role == "developer" and candidate.get("files"):
            score = 5 if attempt == adopted else 4 if attempt in approved else 3 if candidate.get("eligible") else 2
            label = {5:"사용자가 채택한 변경", 4:"독립 리뷰 승인 변경", 3:"검증 통과 후보", 2:"검증 기록이 있는 수정 후보"}[score]
            record("implementation", score, label, "candidate", attempt)
    if role == "reviewer":
        for review in facts["reviews"]:
            attempt = review["attempt"]
            corrected = any(i > attempt and candidates[i]["digest"] != review["digest"] for i in approved)
            if review["status"] == "changes_requested":
                label = "변경 요청 후 다른 후보 승인" if corrected else "후보에서 변경 요청"
                record("review", 5 if corrected else 3, label, "review", attempt)
                record("analysis", 4 if corrected else 2, label, "review", attempt)
            elif attempt in approved:
                record("review", 4 if attempt == adopted else 3, "검증한 후보의 독립 리뷰", "review", attempt)
                if any(c["attempt"] == attempt and json.loads(c["receipt"]).get("status") == "passed"
                       for c in facts["checks"]):
                    record("verification", 4, "테스트 결과가 있는 후보의 독립 리뷰", "review", attempt)
    participated = {c["revision"] for c in facts["contributions"] if c["role"] == role}
    accepted = {r["revision"] for r in facts["revisions"]}
    for rev in facts["revisions"]:
        if role == "coordinator":
            record("analysis", 3, "승인된 범위와 검증 계획", "meeting", revision=rev["revision"])
            if rev["direction_id"]:
                record("collaboration", 4, "추가 지시를 반영한 계획 버전", "meeting", revision=rev["revision"])
    for criterion in facts["criteria"]:
        revision = criterion["revision"]
        if revision in accepted and (role == "coordinator" or revision in participated):
            record("project", 4, "승인 계획의 프로젝트 기준 통과 · " + criterion["criterion"][:100],
                   "meeting", revision=revision)
    for answer in facts["answers"]:
        if answer["role"] == role:
            record("collaboration", 3, "원래 요청에 대한 답변 전달", "directions", answer["attempt"], request_id=answer["id"])
    for direction in facts["directions"]:
        if direction["role"] == role:
            record("collaboration", 4 if direction["attempt"] in approved else 3,
                   "추가 지시 전달 후 후보 승인" if direction["attempt"] in approved else "추가 지시 전달",
                   "directions", direction["attempt"], direction_id=direction["id"])
    return found


def describe(db, workspace, name, role):
    from . import goal_recovery, goal_usage

    since = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).isoformat()
    rows = db.execute("""SELECT r.run_id,r.attempts,r.ended_at,t.request_snapshot AS task,
        s.allow_edits,a.assignment_json,g.adopted_attempt
        FROM studio_room_assignments a JOIN studio_runs s ON s.id=a.run_id JOIN runs r ON r.run_id=s.id
        JOIN tasks t ON t.task_id=r.task_id
        LEFT JOIN studio_goals g ON g.id=s.id WHERE s.workspace_id=? AND r.host_name=?
        AND julianday(r.started_at)>=julianday(?) ORDER BY r.started_at DESC,r.run_id LIMIT ?""",
                      (workspace, platform.node(), since, RUN_LIMIT)).fetchall()
    ids = [r["run_id"] for r in rows]
    facts = _facts(db, ids)
    calls_by_run = defaultdict(list)
    changed = set()
    if ids and role != "coordinator" and goal_usage.exists(db):
        marks = ",".join("?" for _ in ids)
        for call in db.execute(f"""SELECT run_id,engine,actual_model,actual_effort,total_tokens FROM studio_usage_calls
                       WHERE role=? AND run_id IN ({marks})""", (role,*ids)):
            calls_by_run[call["run_id"]].append(call)
        changed = {r[0] for r in db.execute(f"""SELECT DISTINCT run_id FROM studio_usage_actions
            WHERE role=? AND run_id IN ({marks}) AND action='provider_change'""",(role,*ids))}
    axes, cohorts = defaultdict(list), {}
    for row in rows:
        assignment = json.loads(row["assignment_json"])
        if role == "reviewer" and not (row["allow_edits"] or assignment.get("meeting")):
            continue
        rid = row["run_id"]
        for axis, evidence in _evidence(row, facts[rid], role).items():
            axes[axis].append(evidence)
        if role == "coordinator" or not row["ended_at"] or rid in changed:
            continue
        calls = calls_by_run[rid]
        combination = {(c["engine"], c["actual_model"], c["actual_effort"]) for c in calls}
        if len(combination) != 1 or not all(next(iter(combination))):
            continue
        engine, model, effort = next(iter(combination))
        group = "reproduction" if assignment.get("reproduce_required") else "development" if row["allow_edits"] else "read_only"
        entry = cohorts.setdefault((group,engine,model,effort), dict(group=group,engine=engine,model=model,effort=effort,
            samples=0,adopted=0,rework=0,active_seconds=0,tokens=0))
        entry["samples"] += 1
        entry["adopted"] += int(row["adopted_attempt"] is not None)
        entry["rework"] += max(0, row["attempts"]-1)
        if entry["active_seconds"] is not None:
            if assignment.get("goal_policy"):
                entry["active_seconds"] += goal_recovery.timing(db, rid)["active_seconds"]
            else:
                entry["active_seconds"] = None
        total = [c["total_tokens"] for c in calls]
        entry["tokens"] = None if entry["tokens"] is None or None in total else entry["tokens"] + sum(total)
    output = []
    for axis,label in AXES:
        evidence = axes[axis]
        output.append(dict(id=axis,label=label,samples=len(evidence),
                           level=round(median(e["strength"] for e in evidence)) if len(evidence)>=3 else None,
                           evidence=evidence[:6]))
    return dict(name=name,role=role,specialty=SPECIALTIES[role],days=WINDOW_DAYS,run_limit=RUN_LIMIT,
                axes=output,comparisons=list(cohorts.values()))
