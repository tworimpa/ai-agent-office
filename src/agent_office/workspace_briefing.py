"""Read a consistent, host-local snapshot of work, decisions and candidates."""
from itertools import combinations
from datetime import datetime, timedelta
import json
import platform
import re

from . import workspace_coordination as coordination
from .models import ACTIVE, validate_id
from .store import TaskStore, now
from .technical_choices import public_prompt


def route(method, path):
    match = re.fullmatch(r"/rooms/([0-9a-f]{32})/(briefing|coordination/([0-9a-f]{32})|decision-revisions/([0-9a-f]{32}))", path)
    if match and method == ("GET" if match[2] == "briefing" else "POST"):
        return match[1], match[2].split("/")[0], match[3] or match[4]
    return None


def check_reply(binding, status, data, payload):
    from .studio_ssh import RemoteUnavailable
    workspace, action, target = binding
    if status != 200 or not isinstance(data,dict) or data.get("workspace_id") != workspace:
        raise RemoteUnavailable()
    if action == "briefing":
        if not isinstance(data.get("observed_at"), str):
            raise RemoteUnavailable()
        for key in ("decisions","work","candidates","overlaps","decision_options"):
            if not isinstance(data.get(key),list) or not all(isinstance(x,dict) for x in data[key]):
                raise RemoteUnavailable()
        for item in data["work"] + data["candidates"] + data["decisions"]:
            validate_id(item.get("run_id"))
            if not isinstance(item.get("task"),str):
                raise RemoteUnavailable()
        for row in data["decisions"]:
            if row.get("kind") not in ("product","question","permission","plan") or not isinstance(row.get("impacts"),list):
                raise RemoteUnavailable()
    elif action == "coordination":
        receipt = data.get("coordination")
        proposed = {k:v for k,v in payload.items() if k!="revision"}
        if (data.get("run_id") != target or not isinstance(receipt,dict) or type(receipt.get("revision")) is not int
                or {k:v for k,v in receipt.items() if k!="revision"} != coordination.normalize(proposed)):
            raise RemoteUnavailable()
    else:
        receipt = data.get("decision")
        if (not isinstance(receipt,dict) or receipt.get("request_id") != target or
                receipt.get("answer") != payload["answer"].strip() or receipt.get("revision") != payload["revision"]+1):
            raise RemoteUnavailable()


def _short(text, limit=175):
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ""


def _has(db, name):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


def _candidates(db, workspace):
    if not _has(db,"studio_goal_reviews"):
        return {}
    # Only the immutable, independently approved digest qualifies for the review queue.
    rows = db.execute("""SELECT c.run_id,c.attempt,c.record,g.criteria_json,
        r.at AS reviewed_at,substr(r.output,1,360) AS review
        FROM studio_goal_candidates c JOIN studio_goal_reviews r ON r.run_id=c.run_id AND r.attempt=c.attempt
          AND r.digest=json_extract(c.record,'$.digest') AND r.kind='review' AND r.status='approved'
        JOIN studio_goals g ON g.id=c.run_id JOIN runs x ON x.run_id=c.run_id
        WHERE g.workspace_id=? AND x.host_name=? AND g.adopted_attempt IS NULL AND x.ended_at IS NOT NULL AND
          x.state IN ('READY_FOR_HUMAN_REVIEW','FAILED','WAITING_USER') AND
          json_extract(c.record,'$.eligible')=1 AND json_extract(c.record,'$.status')='passed'
        ORDER BY json_extract(c.record,'$.score') DESC,c.attempt DESC""", (workspace,platform.node()))
    result = {}
    for row in rows:
        rid = row["run_id"]
        if rid in result:
            continue
        basis = coordination.stage_status(db, rid, "review", row["attempt"])
        if not basis["ready"]:
            continue
        record = json.loads(row["record"])
        checks = [dict(check) for check in db.execute("""SELECT json_extract(receipt,'$.stage') AS stage,
            json_extract(receipt,'$.status') AS status FROM studio_goal_checks WHERE run_id=? AND attempt=?
            ORDER BY position""", (rid, row["attempt"]))]
        meeting = db.execute("""SELECT criterion,status FROM studio_meeting_checks WHERE run_id=? AND revision=
            (SELECT MAX(revision) FROM studio_meeting_revisions WHERE run_id=?)""",(rid,rid)).fetchall()
        verified = {r["criterion"] for r in meeting if r["status"] == "passed"}
        manual = set(json.loads(row["criteria_json"])) - verified
        manual.update(r["criterion"] for r in meeting if r["status"] != "passed")
        result[rid] = dict(run_id=rid, attempt=row["attempt"], digest=record["digest"],
                           summary=_short(record.get("change") or row["review"]), checks=checks,
                           manual_count=len(manual), reviewed_at=row["reviewed_at"])
    return result


def _owner(role, assignment):
    if role == "tester":
        return "테스트 실행기"
    if role == "coordinator":
        return "조정자"
    return (assignment.get("roles",{}).get(role) or {}).get("name") or {"developer":"개발 담당","reviewer":"검토 담당"}.get(role,role)


def _areas(task, assigned):
    """Only report matching named paths, interfaces and schemas; never infer an execution gate."""
    result = {x.casefold(): (x,"지정한 변경 영역") for x in assigned}
    pattern = r"(?<![\w.-])(?:[\w.-]+/)*[\w-]+\.(?:py|js|mjs|ts|tsx|json|sql|toml|yaml|yml)\b"
    for path in re.findall(pattern, task):
        result.setdefault(path.casefold(), (path, "작업 지시에 명시한 파일"))
    for symbol in re.findall(r"`([A-Za-z][\w.]{1,80}(?:Schema|Config|Interface|Protocol|Settings))`", task):
        result.setdefault(symbol.casefold(), (symbol, "작업 지시에 명시한 인터페이스·자료형"))
    return result


def read(db, workspace):
    observed = now()
    candidates = _candidates(db, workspace)
    rows = db.execute("""SELECT r.run_id,r.state,r.attempts,r.ended_at,r.started_at,t.request_snapshot AS task,
        t.updated_at,s.stop_requested,s.allow_edits,a.assignment_json,g.id AS goal,
        (SELECT timestamp FROM events WHERE run_id=r.run_id ORDER BY seq DESC LIMIT 1) AS event_at
        FROM studio_runs s JOIN runs r ON r.run_id=s.id JOIN tasks t ON t.task_id=r.task_id
        LEFT JOIN studio_room_assignments a ON a.run_id=s.id LEFT JOIN studio_goals g ON g.id=s.id
        WHERE s.workspace_id=? AND r.host_name=?
        ORDER BY (r.ended_at IS NULL) DESC,r.started_at DESC,r.run_id""",(workspace,platform.node())).fetchall()
    # All open work, all candidates, and recent finished context, independent of navigation's history page.
    referenced = {r[0] for r in db.execute("""SELECT DISTINCT d.source_run FROM workspace_dependencies d
        JOIN runs r ON r.run_id=d.run_id JOIN studio_runs s ON s.id=d.run_id
        WHERE s.workspace_id=? AND r.ended_at IS NULL""", (workspace,))}
    kept = [r for i,r in enumerate(rows) if r["ended_at"] is None or i < 40 or r["run_id"] in candidates
            or r["run_id"] in referenced]
    work, decisions, options, areas = {}, [], [], {}
    assignments = {}
    for row in kept:
        rid = row["run_id"]
        assignment = json.loads(row["assignment_json"]) if row["assignment_json"] else {}
        assignments[rid] = assignment
        spec = coordination.config(db,rid)
        state = "STOP_REQUESTED" if row["stop_requested"] and not row["ended_at"] else row["state"]
        last = row["event_at"] or row["updated_at"]
        admitted = db.execute("""SELECT stage,attempt,entered_at,decisions FROM workspace_stage_evidence
            WHERE run_id=? ORDER BY rowid DESC LIMIT 1""",(rid,)).fetchone()
        wait = db.execute("""SELECT stage,attempt,reasons,started_at FROM studio_goal_stage_waits WHERE run_id=?
            AND ended_at IS NULL""",(rid,)).fetchone() if _has(db,"studio_goal_stage_waits") else None
        stage = (wait["stage"] if wait else admitted["stage"] if admitted else
                 {"QUEUED":"start","RUNNING":"implement","TESTING":"verify","REVIEWING":"review"}.get(state))
        reasons = json.loads(wait["reasons"]) if wait else []
        evidence = json.loads(admitted["decisions"]) if admitted else []
        role = {"review":"reviewer","verify":"tester","start":"coordinator"}.get(stage,"developer")
        progress = {"QUEUED":"작업 시작 대기","RUNNING":"담당자 작업 중","TESTING":"검증 중","REVIEWING":"독립 검토 중",
                    "READY_FOR_HUMAN_REVIEW":"후보 검토 대기","RECOVERY_REQUIRED":"복구 확인 필요","FAILED":"실패",
                    "CANCELED":"취소됨","PLAN_ONLY":"조회 완료","WAITING_USER":"사용자 확인 대기"}.get(state,state)
        proof = ""
        if _has(db, "studio_goal_checks"):
            reproduction = db.execute("""SELECT json_extract(receipt,'$.failed_ids[0]') FROM studio_goal_checks
                WHERE run_id=? AND attempt=0 AND json_extract(receipt,'$.status')='failed'
                AND json_extract(receipt,'$.failed_ids[0]') IS NOT NULL
                ORDER BY position DESC LIMIT 1""",(rid,)).fetchone()
            if reproduction:
                proof = "원본 실패 재현 · " + _short(reproduction[0].rsplit("::",1)[-1], 85)
            check = db.execute("""SELECT receipt FROM studio_goal_checks WHERE run_id=? ORDER BY attempt DESC,
                json_extract(receipt,'$.started_at') DESC LIMIT 1""",(rid,)).fetchone()
            if check:
                check = json.loads(check[0])
                when = datetime.fromisoformat(check["started_at"])
                if check["status"] != "running":
                    when += timedelta(seconds=check.get("elapsed_seconds",0))
                last = max(datetime.fromisoformat(last),when).isoformat()
                if check["attempt"] == 0 and check["status"] == "failed" and not row["ended_at"]:
                    progress = "기준 커밋에서 실패 재현"
                elif state == "TESTING" and check["status"] == "running":
                    progress = "검증 진행 중"
        if wait:
            last = max(last,wait["started_at"])
            gate = coordination.stage_status(db,rid,wait["stage"],wait["attempt"])
            reasons = gate["reasons"]
            if gate["ready"]:
                progress = "선행 조건 충족 · 재개 대기"
        elif admitted:
            last = max(last,admitted["entered_at"])
        work[rid] = dict(run_id=rid, task=_short(row["task"]), role=role, owner=_owner(role,assignment), state=state,
                         progress=progress, proof=proof, last_progress_at=last, stage=stage, wait_reasons=reasons,
                         decisions=evidence, coordination_revision=spec["revision"], priority=spec["priority"],
                         areas=spec["areas"], dependencies=spec["dependencies"], active_stage=bool(admitted and not wait and not row["ended_at"]),
                         allow_edits=bool(row["allow_edits"]),
                         editable=bool(spec["revision"] and not row["ended_at"] and not row["stop_requested"]))
        if not row["ended_at"]:
            areas[rid] = _areas(row["task"],spec["areas"])
        if rid in candidates:
            candidates[rid]["task"] = work[rid]["task"]

    def impacts(source, request, prompt, kind):
        own = work[source]
        found = [dict(run_id=source,task=own["task"],stage=own["stage"] or "implement")]
        if kind in {"product","plan"}:
            for rid,item in work.items():
                if rid == source or not item["editable"]:
                    continue
                for edge in item["dependencies"]:
                    if edge["source_run"] != source or edge["condition"] != "product_decision":
                        continue
                    selected = edge.get("request_id","")
                    if not selected and kind == "plan":
                        selected = coordination._plan_by_prompt(db,source,edge["question"]) or ""
                    pending_plan = (coordination.plan_selection(db,source,selected) if
                                    kind=="plan" and coordination.PLAN_KEY.fullmatch(selected) else None)
                    if (selected == request or pending_plan and not pending_plan["answer"] or
                            not selected and kind=="product" and edge.get("question")==prompt):
                        link = dict(run_id=rid,task=item["task"],stage=edge["stage"],
                                    progress=item["progress"],proof=item["proof"])
                        if link not in found:
                            found.append(link)
        return found

    for row in db.execute("""SELECT q.id,q.run_id,q.role,q.kind,q.decision_type,q.prompt,q.status,q.created_at,q.decided_at,q.delivered_at
        FROM studio_requests q JOIN studio_runs s ON q.run_id=s.id WHERE s.workspace_id=?
        AND (q.status='pending' OR q.decision_type='product_decision' AND q.status IN ('decided','delivered'))
        AND NOT EXISTS (SELECT 1 FROM workspace_decision_uses WHERE target_request=q.id)
        ORDER BY q.created_at,q.id""",(workspace,)):
        rid = row["run_id"]
        if rid not in work:
            continue
        work[rid]["last_progress_at"] = max(work[rid]["last_progress_at"],row["delivered_at"] or row["decided_at"] or row["created_at"])
        kind = "product" if row["decision_type"] == "product_decision" else row["kind"]
        prompt = public_prompt(row["prompt"]) if kind=="question" else row["prompt"]
        if kind == "product":
            option = dict(source_run=rid,request_id=row["id"],prompt=prompt)
            effective = coordination.resolve_decision(db, option)
            if effective:
                option.update(effective)
                change = db.execute("SELECT reason FROM workspace_decision_revisions WHERE request_id=? AND revision=?",
                                    (row["id"],effective["revision"])).fetchone()
                if change:
                    option["reason"] = change[0]
            options.append(option)
        if row["status"] == "pending" and work[rid]["state"] in ACTIVE:
            decisions.append(dict(run_id=rid,request_id=row["id"],kind=kind,role=row["role"],owner=_owner(row["role"],assignments[rid]),
                                  task=work[rid]["task"],prompt=prompt,created_at=row["created_at"],
                                  impacts=impacts(rid,row["id"],prompt,kind)))
    for row in db.execute("""SELECT m.run_id,m.status,m.revision,v.plan,v.created_at FROM studio_meetings m
        JOIN studio_meeting_revisions v ON m.run_id=v.run_id AND m.revision=v.revision
        JOIN studio_runs s ON s.id=m.run_id WHERE s.workspace_id=?""",(workspace,)):
        rid = row["run_id"]
        if rid not in work:
            continue
        questions = json.loads(row["plan"]).get("questions",[])
        key = "plan:" + str(row["revision"])
        for i,q in enumerate(questions,1):
            options.append(dict(source_run=rid,request_id=f"{key}:q{i}",prompt=q["question"]))
        if row["status"]=="awaiting_review" and work[rid]["state"] in ACTIVE:
            prompt = questions[0]["question"] if questions else "작업 계획을 확인해 주세요."
            if len(questions)>1:
                prompt += f" 외 {len(questions)-1}건"
            decisions.append(dict(run_id=rid,request_id=key,revision=row["revision"],kind="plan",role="coordinator",
                                  owner="조정자",task=work[rid]["task"],prompt=prompt,created_at=row["created_at"],
                                  impacts=impacts(rid,key,prompt,"plan")))
    overlaps = []
    for left,right in combinations(areas,2):
        for key in sorted(areas[left].keys() & areas[right].keys()):
            area, basis = areas[left][key]
            overlaps.append(dict(left=left,right=right,area=area,reason=basis + " · " + areas[right][key][1] +
                                 " 일치. 변경 내용의 충돌 여부는 미확인"))
    return dict(workspace_id=workspace,observed_at=observed,
                decisions=sorted(decisions,key=lambda r:(-len(r["impacts"]),r["created_at"],r["request_id"])),
                candidates=sorted(candidates.values(),key=lambda c:c["reviewed_at"],reverse=True),
                work=sorted(work.values(),key=lambda w:(not bool(w["wait_reasons"]),-w["priority"])),
                overlaps=overlaps,decision_options=options)


def handle(server, workspace, action, target, data):
    server.catalog.get(workspace)
    with TaskStore(server.db_path, existing=True, readonly=data is None) as store:
        db = store.connection
        if data is None:
            db.execute("BEGIN")
            return read(db, workspace)
        with store._write():
            if action == "coordination":
                payload = dict(data)
                revision = payload.pop("revision", None)
                result = coordination.save(db, workspace, target, payload, revision)
                return dict(workspace_id=workspace,run_id=target,coordination=result)
            return dict(workspace_id=workspace,decision=coordination.revise(db,workspace,target,data))
