"""Workspace-scoped dependencies and the immutable evidence admitted at each stage."""
import json
import platform
import re

from .events import snapshot_request
from .models import ACTIVE, OfficeError, validate_id
from .store import encode, now


STAGES = ("start", "reproduce", "implement", "verify", "review")
STAGE_NAMES = dict(zip(STAGES, ("시작", "재현", "구현", "검증", "리뷰")))
DEFAULT = {"priority": 0, "areas": [], "dependencies": []}
PLAN_KEY = re.compile(r"plan:([1-9][0-9]{0,7}):(q[1-9][0-9]?)")


def ensure_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS workspace_coordination(
        run_id TEXT PRIMARY KEY REFERENCES studio_runs(id), priority INTEGER NOT NULL,
        areas TEXT NOT NULL, revision INTEGER NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS workspace_dependencies(
        run_id TEXT NOT NULL REFERENCES workspace_coordination(run_id), source_run TEXT NOT NULL REFERENCES studio_runs(id),
        stage TEXT NOT NULL, spec TEXT NOT NULL, PRIMARY KEY(run_id,source_run,stage,spec))""")
    db.execute("CREATE INDEX IF NOT EXISTS workspace_dependents ON workspace_dependencies(source_run)")
    db.execute("""CREATE TABLE IF NOT EXISTS workspace_stage_evidence(
        run_id TEXT NOT NULL REFERENCES workspace_coordination(run_id), stage TEXT NOT NULL,
        attempt INTEGER NOT NULL, entered_at TEXT NOT NULL, decisions TEXT NOT NULL, dependencies TEXT NOT NULL,
        PRIMARY KEY(run_id,stage,attempt))""")
    db.execute("""CREATE TABLE IF NOT EXISTS workspace_coordination_history(
        run_id TEXT NOT NULL REFERENCES workspace_coordination(run_id), revision INTEGER NOT NULL,
        recorded_at TEXT NOT NULL, spec TEXT NOT NULL, PRIMARY KEY(run_id,revision))""")
    db.execute("""CREATE TABLE IF NOT EXISTS workspace_decision_revisions(
        request_id TEXT NOT NULL REFERENCES studio_requests(id), revision INTEGER NOT NULL,
        op_id TEXT NOT NULL UNIQUE, answer TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(request_id,revision))""")
    db.execute("""CREATE TABLE IF NOT EXISTS workspace_decision_uses(
        target_request TEXT PRIMARY KEY REFERENCES studio_requests(id), source_run TEXT NOT NULL,
        source_request TEXT NOT NULL, revision INTEGER NOT NULL, evidence TEXT NOT NULL)""")


def _text(value, limit):
    if (not isinstance(value, str) or not value.strip() or len(value.encode("utf8")) > limit or
            "\0" in value or value != snapshot_request(value)):
        raise ValueError("작업 조정에 사용할 문구와 길이를 확인하세요.")
    return value.strip()


def normalize(value):
    if not isinstance(value, dict) or value.keys() - DEFAULT.keys():
        raise ValueError("작업 우선순위와 의존 조건을 확인하세요.")
    rank = value.get("priority", 0)
    areas, edges = value.get("areas", []), value.get("dependencies", [])
    if type(rank) is not int or not -9 <= rank <= 9:
        raise ValueError("우선순위는 -9부터 9까지의 정수입니다.")
    if not isinstance(areas, list) or len(areas) > 16 or not isinstance(edges, list) or len(edges) > 16:
        raise ValueError("변경 영역과 선행 조건은 각각 16개까지 지정할 수 있습니다.")
    normalized = []
    selectors = set()
    for edge in edges:
        if not isinstance(edge, dict) or edge.keys() - {"source_run","stage","condition","request_id","question","reason"}:
            raise ValueError("선행 조건 형식을 확인하세요.")
        source = validate_id(edge.get("source_run"))
        stage, condition = edge.get("stage"), edge.get("condition")
        if stage not in STAGES or condition not in ("completion", "product_decision"):
            raise ValueError("대기할 단계와 조건을 확인하세요.")
        item = dict(source_run=source, stage=stage, condition=condition, reason=_text(edge.get("reason"), 500))
        request, question = edge.get("request_id"), edge.get("question")
        if condition == "product_decision":
            if bool(request) == bool(question):
                raise ValueError("공유할 제품 질문의 ID 또는 정확한 질문 문구 하나를 지정하세요.")
            if request:
                if not isinstance(request, str) or not (PLAN_KEY.fullmatch(request) or re.fullmatch("[a-f0-9]{32}",request)):
                    raise ValueError("제품 결정 ID를 확인하세요.")
                item["request_id"] = request
            else:
                item["question"] = _text(question, 1024)
        elif request or question:
            raise ValueError("완료 조건에는 질문을 지정할 수 없습니다.")
        identity = (source, stage, condition, item.get("request_id"), item.get("question"))
        if identity in selectors:
            raise ValueError("중복된 선행 조건입니다.")
        selectors.add(identity)
        normalized.append(item)
    return dict(priority=rank, areas=list(dict.fromkeys(_text(a,180) for a in areas)),
                dependencies=sorted(normalized, key=encode))


def markdown_settings(text):
    """Optional structured intake. Overlapping prose is a hint, never a blocking edge."""
    section = False
    fence = None
    result = {}
    for line in text.splitlines():
        if re.match(r"^ {0,3}(`{3,}|~{3,})",line):
            marker = line.strip()[0]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence:
            continue
        if line.startswith("#"):
            section = line.strip().casefold() in ("## 작업 조정","## office coordination")
            continue
        if not section or not line.strip():
            continue
        label, separator, value = line.strip().removeprefix("- ").partition(":")
        value = value.strip()
        if not separator or not value:
            raise ValueError("작업 조정 항목의 이름과 값을 확인하세요.")
        if label in ("우선순위","priority"):
            if not re.fullmatch(r"-?[0-9]",value):
                raise ValueError("우선순위를 -9~9로 입력하세요.")
            result["priority"] = int(value)
        elif label in ("변경 영역","area"):
            result.setdefault("areas",[]).append(value)
        elif label in ("선행","depends"):
            parts = [p.strip() for p in value.split("|")]
            if len(parts) not in (4,5):
                raise ValueError("선행: 원본 목표 | 대기 단계 | 조건 | 이유 형식을 확인하세요.")
            ref, stage, condition = parts[:3]
            stage = {v:k for k,v in STAGE_NAMES.items()}.get(stage,stage)
            condition = {"완료":"completion","제품 결정":"product_decision"}.get(condition,condition)
            edge = dict(source_run=ref,stage=stage,condition=condition,reason=parts[-1])
            if condition == "product_decision" and len(parts)==5:
                key = parts[3]
                edge["request_id" if PLAN_KEY.fullmatch(key) or re.fullmatch("[a-f0-9]{32}",key) else "question"] = key
            elif condition != "completion" or len(parts)!=4:
                raise ValueError("제품 결정에는 요청 ID 또는 원래 질문을 입력하세요.")
            result.setdefault("dependencies",[]).append(edge)
        else:
            raise ValueError("작업 조정 항목은 우선순위, 변경 영역, 선행입니다.")
    return result or None


def bind_references(db, workspace, spec, origin=None):
    value = dict(spec)
    edges = []
    if not isinstance(value.get("dependencies", []), list):
        raise ValueError("선행 조건 목록을 확인하세요.")
    for entry in value.get("dependencies",[]):
        if not isinstance(entry, dict):
            raise ValueError("선행 조건 형식을 확인하세요.")
        edge = dict(entry)
        ref = edge.get("source_run")
        if isinstance(ref,str) and re.fullmatch(r"#[1-9][0-9]{0,15}",ref):
            query = """SELECT o.run_id,o.goal_id FROM studio_external_origins o JOIN studio_runs s ON
                s.id=o.run_id WHERE s.workspace_id=? AND o.number=?"""
            params = [workspace,int(ref[1:])]
            if origin:
                query += " AND o.instance=? AND o.repository_id=?"
                params.extend((origin["instance"],origin["repository_id"]))
            rows = db.execute(query+" ORDER BY o.created_at DESC,o.rowid DESC",params).fetchall()
            if not rows or len({r["goal_id"] for r in rows}) != 1:
                raise ValueError("선행 Issue를 이 작업공간에 먼저 연결하거나 정확한 실행 ID를 지정하세요.")
            edge["source_run"] = rows[0]["run_id"]
        edges.append(edge)
    value["dependencies"] = edges
    return normalize(value)


def _exists(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='workspace_coordination'").fetchone())


def config(db, run_id):
    row = db.execute("SELECT * FROM workspace_coordination WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        return dict(priority=0, areas=[], dependencies=[], revision=0)
    edges = [json.loads(r[0]) for r in db.execute(
        "SELECT spec FROM workspace_dependencies WHERE run_id=? ORDER BY spec", (run_id,))]
    return dict(priority=row["priority"], areas=json.loads(row["areas"]), dependencies=edges, revision=row["revision"])


def priority(db, run_id):
    if _exists(db):
        row = db.execute("SELECT priority FROM workspace_coordination WHERE run_id=?", (run_id,)).fetchone()
        if row:
            return row[0]
    return 0


def _run(db, run_id):
    row = db.execute("""SELECT r.run_id,r.state,r.outcome,r.attempts,r.ended_at,s.workspace_id,s.stop_requested,
        t.request_snapshot AS task FROM runs r JOIN studio_runs s ON s.id=r.run_id JOIN tasks t ON t.task_id=r.task_id
        WHERE r.run_id=? AND r.host_name=?""", (run_id, platform.node())).fetchone()
    if row is None:
        raise OfficeError("NOT_FOUND")
    return row


def _validate_source(db, workspace_id, edge, target):
    source = edge["source_run"]
    if _run(db, source)["workspace_id"] != workspace_id:
        raise ValueError("같은 작업공간의 선행 목표를 선택하세요.")
    if source == target or db.execute("""WITH RECURSIVE upstream(id) AS (
        SELECT source_run FROM workspace_dependencies WHERE run_id=? UNION
        SELECT d.source_run FROM workspace_dependencies d JOIN upstream u ON u.id=d.run_id)
        SELECT 1 FROM upstream WHERE id=? LIMIT 1""", (source,target)).fetchone():
        raise ValueError("서로 기다리는 순환 의존성입니다. 선행 조건을 바꿔 주세요.")
    req = edge.get("request_id")
    if req and (match := PLAN_KEY.fullmatch(req)):
        row = db.execute("SELECT plan FROM studio_meeting_revisions WHERE run_id=? AND revision=?",
                         (source,int(match[1]))).fetchone()
        if not row or len(json.loads(row[0]).get("questions", [])) < int(match[2][1:]):
            raise ValueError("원래 계획의 제품 질문을 찾을 수 없습니다.")
    elif req and not db.execute("""SELECT 1 FROM studio_requests WHERE id=? AND run_id=? AND kind='question'
            AND decision_type='product_decision' AND NOT EXISTS
            (SELECT 1 FROM workspace_decision_uses WHERE target_request=studio_requests.id)""", (req,source)).fetchone():
        raise ValueError("선행 목표의 제품 결정만 공유할 수 있습니다.")


def save(db, workspace_id, run_id, value, expected_revision, *, admission=False):
    """Called inside the same transaction as admission or an operator's edit."""
    spec = normalize(value)
    target = _run(db, run_id)
    if target["workspace_id"] != workspace_id:
        raise OfficeError("NOT_FOUND")
    if not db.execute("SELECT 1 FROM studio_room_assignments WHERE run_id=?", (run_id,)).fetchone():
        raise ValueError("조정자에게 맡긴 작업의 단계만 조정할 수 있습니다.")
    edits = db.execute("SELECT allow_edits FROM studio_runs WHERE id=?", (run_id,)).fetchone()[0]
    if not edits and any(edge["stage"] != "start" for edge in spec["dependencies"]):
        raise ValueError("조회 작업에는 시작 단계 선행 조건만 지정할 수 있습니다.")
    old = config(db, run_id)
    if not old["revision"] and not admission:
        raise ValueError("기존 실행은 기록을 열 수 있습니다. 새 의뢰에서 조정 조건을 지정하세요.")
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("검토한 조정 버전을 확인하세요.")
    if old["revision"] > 0 and spec == {key:old[key] for key in DEFAULT}:
        return old
    if old["revision"] != expected_revision:
        raise OfficeError("REQUEST_CONFLICT")
    if target["ended_at"] or target["stop_requested"]:
        raise OfficeError("REQUEST_CLOSED")
    for edge in spec["dependencies"]:
        _validate_source(db, workspace_id, edge, run_id)
    changed = {encode(edge) for edge in old["dependencies"]} ^ {encode(edge) for edge in spec["dependencies"]}
    parked = db.execute("SELECT stage FROM studio_goal_stage_waits WHERE run_id=? AND ended_at IS NULL",
                        (run_id,)).fetchone() if changed else None
    for value in changed:
        stage = json.loads(value)["stage"]
        already = db.execute("SELECT stage FROM workspace_stage_evidence WHERE run_id=?", (run_id,)).fetchall()
        if any(STAGES.index(r[0]) >= STAGES.index(stage) for r in already) and not (parked and parked[0] in ("verify","review")):
            raise ValueError("이미 시작한 단계의 선행 조건은 소급 변경할 수 없습니다.")
    revision = old["revision"] + 1
    db.execute("""INSERT INTO workspace_coordination VALUES(?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET
        priority=excluded.priority,areas=excluded.areas,revision=excluded.revision""",
               (run_id,spec["priority"],encode(spec["areas"]),revision))
    db.execute("DELETE FROM workspace_dependencies WHERE run_id=?", (run_id,))
    db.executemany("INSERT INTO workspace_dependencies VALUES(?,?,?,?)",
                   ((run_id,e["source_run"],e["stage"],encode(e)) for e in spec["dependencies"]))
    db.execute("INSERT INTO workspace_coordination_history VALUES(?,?,?,?)",(run_id,revision,now(),encode(spec)))
    return dict(spec, revision=revision)


def plan_selection(db, source, request):
    match = PLAN_KEY.fullmatch(request)
    rows = db.execute("""SELECT revision,plan,answers FROM studio_meeting_revisions WHERE run_id=? AND revision>=?
        ORDER BY revision""", (source,int(match[1]))).fetchall()
    if not rows or rows[0]["revision"] != int(match[1]):
        return None
    choices = json.loads(rows[0]["plan"]).get("questions", [])
    index = int(match[2][1:])-1
    if index >= len(choices):
        return None
    question = choices[index]
    selected = None
    for row in rows:
        current = json.loads(row["plan"]).get("questions", [])
        matches = [i for i,q in enumerate(current,1) if q == question]
        if len(matches) != 1:
            selected = None
            continue
        answer = json.loads(row["answers"]).get(f"q{matches[0]}")
        if selected is None or selected["answer"] != answer:
            selected = dict(source_run=source,request_id=request,prompt=question["question"],
                            answer=answer,revision=row["revision"])
    return selected


def _plan_by_prompt(db, source, prompt):
    key = choice = None
    for row in db.execute("SELECT revision,plan FROM studio_meeting_revisions WHERE run_id=? ORDER BY revision",
                          (source,)):
        matches = [(i,q) for i,q in enumerate(json.loads(row["plan"]).get("questions",[]),1) if q["question"] == prompt]
        if len(matches) > 1:
            key, choice = "ambiguous", None
        elif not matches:
            key = choice = None
        elif matches[0][1] != choice:
            index,choice = matches[0]
            key = f"plan:{row['revision']}:q{index}"
    return key


def resolve_decision(db, edge):
    source = edge["source_run"]
    request = edge.get("request_id")
    if request and PLAN_KEY.fullmatch(request):
        value = plan_selection(db, source, request)
        return value if value and value["answer"] else None
    query, param = ("id=?",request) if request else ("prompt=?",edge["question"])
    rows = db.execute(f"""SELECT * FROM studio_requests WHERE run_id=? AND kind='question' AND
        decision_type='product_decision' AND status!='expired' AND {query} AND NOT EXISTS
        (SELECT 1 FROM workspace_decision_uses WHERE target_request=studio_requests.id)
        ORDER BY created_at LIMIT 2""", (source,param)).fetchall()
    if not request:
        plan_key = _plan_by_prompt(db,source,edge["question"])
        if plan_key:
            if rows or plan_key == "ambiguous":
                return None
            value = plan_selection(db,source,plan_key)
            return value if value and value["answer"] else None
    if len(rows) != 1 or rows[0]["status"] not in ("decided","delivered") or rows[0]["decision"] != "answer":
        return None
    row = rows[0]
    version = db.execute("SELECT revision,answer FROM workspace_decision_revisions WHERE request_id=? "
                         "ORDER BY revision DESC LIMIT 1", (row["id"],)).fetchone()
    return dict(source_run=source,request_id=row["id"],prompt=row["prompt"],
                answer=version["answer"] if version else row["answer"],revision=version["revision"] if version else 1)


def stage_status(db, run_id, stage, attempt, *, _visiting=frozenset(), _completion=None):
    if stage not in STAGES:
        raise ValueError("알 수 없는 작업 단계입니다.")
    result = dict(ready=True,reasons=[],decisions=[],dependencies=[],revision=0,stale=False,rework_required=False)
    if _completion is None:
        _completion = {}
    if run_id in _visiting:
        result.update(ready=False,reasons=["선행 목표의 순환 관계를 확인하세요."])
        return result
    if not _exists(db):
        return result
    spec = config(db, run_id)
    result["revision"] = spec["revision"]
    if not spec["revision"]:
        return result
    # The source worker is also accountable for a changed decision it received.
    own = db.execute("""SELECT id FROM studio_requests WHERE run_id=? AND decision_type='product_decision'
        AND status='delivered' AND decision='answer' AND id NOT IN
        (SELECT target_request FROM workspace_decision_uses)""", (run_id,))
    for row in own:
        original = resolve_decision(db, dict(source_run=run_id,request_id=row[0]))
        if original:
            result["decisions"].append(original)
    seen = set()
    for edge in spec["dependencies"]:
        if STAGES.index(edge["stage"]) > STAGES.index(stage):
            continue
        basis = edge.copy()
        result["dependencies"].append(basis)
        source = _run(db, edge["source_run"])
        reason = edge["reason"]
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='studio_external_origins'").fetchone():
            successor = db.execute("""SELECT b.run_id FROM studio_external_origins a JOIN studio_external_origins b
                ON a.goal_id=b.goal_id WHERE a.run_id=? ORDER BY b.created_at DESC,b.rowid DESC LIMIT 1""",
                                   (edge["source_run"],)).fetchone()
            if successor and successor[0] != source["run_id"]:
                if edge["condition"] == "product_decision":
                    result["reasons"].append(reason + " · 선행 목표에 새 실행이 있어 제품 결정을 다시 지정해야 합니다.")
                    continue
                source = _run(db,successor[0])
                basis["effective_source"] = successor[0]
        if source["stop_requested"] or source["state"] in {"CANCELED","RECOVERY_REQUIRED","FAILED"}:
            result["reasons"].append(reason + " · 선행 작업 중단, 복구 또는 새 선행 목표 지정 필요")
        elif edge["condition"] == "completion":
            if source["state"] not in {"READY_FOR_HUMAN_REVIEW","PLAN_ONLY"}:
                result["reasons"].append(reason + " · 선행 작업 완료 대기")
            else:
                source_id = source["run_id"]
                if source_id not in _completion:
                    _completion[source_id] = stage_status(db,source_id,"review",source["attempts"],
                        _visiting=_visiting | {run_id}, _completion=_completion)["ready"]
                if not _completion[source_id]:
                    result["reasons"].append(reason + " · 선행 목표의 완료 근거가 바뀌어 재검증이 필요합니다.")
        else:
            decision = resolve_decision(db, edge)
            if decision is None:
                result["reasons"].append(reason + " · 선행 제품 결정 대기")
            elif (decision["source_run"],decision["request_id"]) not in seen:
                seen.add((decision["source_run"],decision["request_id"]))
                result["decisions"].append(decision)
    result["ready"] = not result["reasons"]
    if result["ready"] and stage in ("verify","review"):
        current = {(r["source_run"],r["request_id"]):r for r in result["decisions"]}
        for row in db.execute("""SELECT decisions,dependencies,stage FROM workspace_stage_evidence WHERE run_id=? AND
                (stage IN ('implement','verify','review') OR stage='start' AND EXISTS
                (SELECT 1 FROM studio_runs s WHERE s.id=? AND s.allow_edits=0)) AND attempt=?""", (run_id,run_id,attempt)):
            intended = [e for e in result["dependencies"] if STAGES.index(e["stage"]) <= STAGES.index(row["stage"])]
            if (intended != json.loads(row["dependencies"]) or
                    any(current.get((old["source_run"],old["request_id"])) != old for old in json.loads(row["decisions"]))):
                result.update(ready=False,stale=True,rework_required=True)
                result["reasons"].append("적용한 제품 결정이 바뀌었습니다. 새 결정으로 구현과 검증이 필요합니다.")
                break
        if result["ready"]:
            for row in db.execute("""SELECT q.id,q.answer FROM studio_requests q WHERE q.run_id=? AND q.attempt=?
                AND q.decision_type='product_decision' AND q.status='delivered' AND NOT EXISTS
                (SELECT 1 FROM workspace_decision_uses WHERE target_request=q.id) AND EXISTS
                (SELECT 1 FROM workspace_decision_revisions v WHERE v.request_id=q.id)""", (run_id,attempt)):
                if current[(run_id,row["id"])]["answer"] != row["answer"]:
                    result.update(ready=False, stale=True, rework_required=True)
                    result["reasons"].append("담당자가 전달받은 제품 결정이 수정됐습니다.")
                    break
    return result


def try_stage(db, run_id, stage, attempt):
    result = stage_status(db, run_id, stage, attempt)
    if result["ready"] and result["revision"]:
        db.execute("""INSERT OR IGNORE INTO workspace_stage_evidence VALUES(?,?,?,?,?,?)""",
                   (run_id,stage,attempt,now(),encode(result["decisions"]),encode(result["dependencies"])))
    return result


def reuse_product_answer(db, run_id, attempt, target, prompt):
    """Answer only an explicitly shared, still-current product question within this attempt."""
    if not _exists(db):
        return False
    basis = db.execute("""SELECT decisions FROM workspace_stage_evidence WHERE run_id=? AND attempt=?
        AND stage IN ('start','implement','review') ORDER BY rowid DESC LIMIT 1""", (run_id,attempt)).fetchone()
    if not basis:
        return False
    matches = [d for d in json.loads(basis[0]) if d["prompt"] == prompt]
    if len(matches) != 1:
        return False
    decision = matches[0]
    source = _run(db,decision["source_run"])
    if (source["state"] in {"CANCELED","RECOVERY_REQUIRED","FAILED"} or source["stop_requested"] or
            resolve_decision(db,decision) != decision):
        return False
    claimed = db.execute("UPDATE studio_requests SET status='decided',decision='answer',answer=?,decided_at=? "
                         "WHERE id=? AND run_id=? AND attempt=? AND prompt=? AND status='pending' "
                         "AND kind='question' AND decision_type='product_decision'",
                         (decision["answer"],now(),target,run_id,attempt,prompt))
    if claimed.rowcount != 1:
        return False
    db.execute("INSERT INTO workspace_decision_uses VALUES(?,?,?,?,?)",
               (target,decision["source_run"],decision["request_id"],decision["revision"],encode(decision)))
    return True


def revise(db, workspace_id, request_id, payload):
    if not isinstance(payload, dict) or set(payload) != {"id","revision","answer","reason"}:
        raise ValueError("수정할 결정의 버전과 이유를 입력하세요.")
    op = validate_id(payload["id"])
    version = payload["revision"]
    if type(version) is not int or version < 1:
        raise ValueError("결정 버전을 확인하세요.")
    answer, reason = _text(payload["answer"],8192), _text(payload["reason"],500)
    row = db.execute("""SELECT q.* FROM studio_requests q JOIN studio_runs s ON s.id=q.run_id
        WHERE q.id=? AND s.workspace_id=? AND q.decision_type='product_decision' AND q.kind='question'
        AND q.decision='answer' AND q.status IN ('decided','delivered') AND NOT EXISTS
        (SELECT 1 FROM workspace_decision_uses WHERE target_request=q.id)""", (request_id, workspace_id)).fetchone()
    if not row:
        raise OfficeError("REQUEST_CLOSED")
    old = db.execute("SELECT * FROM workspace_decision_revisions WHERE op_id=?", (op,)).fetchone()
    if old:
        if (old["request_id"],old["revision"],old["answer"],old["reason"]) != (request_id,version+1,answer,reason):
            raise OfficeError("REQUEST_CONFLICT")
    else:
        effective = resolve_decision(db, dict(source_run=row["run_id"], request_id=request_id))
        if effective["revision"] != version or effective["answer"] == answer:
            raise OfficeError("REQUEST_CONFLICT")
        db.execute("INSERT INTO workspace_decision_revisions VALUES(?,?,?,?,?,?)",
                   (request_id,version+1,op,answer,reason,now()))
    return dict(request_id=request_id,source_run=row["run_id"],revision=version+1,answer=answer,reason=reason)
