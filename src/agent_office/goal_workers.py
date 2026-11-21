"""Admit provider work at durable goal boundaries and journal the selected provider."""
from datetime import datetime, timedelta, timezone
import json
import re
import threading
from uuid import uuid4

from . import goal_usage as usage
from .store import TaskStore, now


class OperatorStop(Exception):
    def __init__(self, outcome, reason):
        super().__init__(reason)
        self.outcome = outcome


def assignment(db, run_id):
    row = db.execute("""SELECT r.assignment_json,t.developer_engine,t.reviewer_engine FROM studio_room_assignments r
        JOIN runs s ON s.run_id=r.run_id JOIN tasks t ON t.task_id=s.task_id WHERE r.run_id=?""", (run_id,)).fetchone()
    if row:
        return json.loads(row[0]), {"developer":row[1], "reviewer":row[2]}
    return {}, {}


def budget(db,run_id,profile):
    limit = profile.get("token_budget")
    if limit is None:
        return None
    rows = db.execute("""SELECT total_tokens,finished_at FROM studio_usage_calls WHERE run_id=?""", (run_id,))
    consumed = 0
    for total,finished in rows:
        if finished is None:
            return "TOKEN_BUDGET_UNVERIFIED", "진행 중이거나 중단된 호출의 최종 사용량을 확인해야 합니다."
        if total is None:
            return "TOKEN_BUDGET_UNVERIFIED", "공급자가 토큰 합계를 보고하지 않아 남은 목표 예산을 확인할 수 없습니다."
        consumed += total
    if consumed >= limit:
        return "TOKEN_BUDGET_EXHAUSTED", f"목표 토큰 예산 {limit}개 중 {consumed}개를 사용했습니다."
    return None


def choices(profile, engines, role):
    first = dict(profile.get("roles",{}).get(role,{}),engine=engines[role])
    return [first,*profile.get("fallbacks",{}).get(role,[])]


def select(db,run_id,role,profile,engines):
    first_block = None
    for choice in choices(profile,engines,role):
        block = usage.blockers(db,run_id,role,choice)
        if block is None:
            return choice,first_block
        if first_block is None:
            first_block = block
    return None,first_block


def stage_roles(stage, spec):
    if stage == "start" and spec.get("meeting"):
        return ("developer","reviewer")
    if stage == "implement" or stage == "reproduce" and spec.get("reproduce_required"):
        return ("developer",)
    return ("reviewer",) if stage in ("review","consultation") else ()


def reason_for(block):
    return f'{block["engine"]} 공급자 한도 ({block["limit_id"]} / {block["name"]}) · 동일 계정의 사용 가능 응답 대기'


def retry_choice(db, run_id, stage, attempt, choice, profile):
    """Escalate one admitted implementation only, on a documented alternative and a repeated test."""
    effort = profile.get("retry_effort")
    if stage != "implement" or attempt < 2 or not effort or not profile.get("retry_deep") or choice["engine"] != "codex":
        return choice
    previous = db.execute("SELECT attempt FROM studio_usage_actions WHERE run_id=? AND action='retry_effort'",
                          (run_id,)).fetchone()
    if previous:
        return dict(choice,effort=effort) if previous["attempt"] == attempt else choice
    row = db.execute("SELECT record FROM studio_goal_candidates WHERE run_id=? AND attempt=?",(run_id,attempt-1)).fetchone()
    if not row:
        return choice
    candidate = json.loads(row[0])
    checks = [json.loads(r[0]) for r in db.execute(
        "SELECT receipt FROM studio_goal_checks WHERE run_id=? AND attempt=?", (run_id,attempt-1))]
    repeated = [case for check in checks for case in check.get("still_failing", [])]
    if not repeated:
        return choice
    pending = db.execute("""SELECT 1 FROM studio_requests WHERE run_id=? AND status='pending'
        AND (kind='permission' OR decision_type='product_decision') LIMIT 1""",(run_id,)).fetchone()
    hypothesis = re.search(r"^다음 가설\s*[:：]\s*(.{6,300})$",candidate.get("hypothesis",""),re.MULTILINE)
    if pending:
        reason = "제품 결정·권한 응답을 기다리는 요청이 있습니다. 설정을 유지합니다."
    elif any(c.get("status") not in {"passed","failed"} or c.get("scope_changes") for c in checks):
        reason = "검증 환경 또는 범위가 달라져 결과를 비교할 수 없습니다. 설정을 유지합니다."
    elif hypothesis is None or not any(name in hypothesis[1] for name in candidate.get("files",[]) + repeated):
        reason = "같은 검사가 실패했지만 변경 파일 또는 실패 검사에 연결된 다음 가설이 없습니다. 설정을 유지합니다."
    else:
        reason = f"사전 위임 · 반복 실패 {repeated[0][:120]} · 다음 가설 {hypothesis[1][:200]} · 이번 시도 {effort}"
        usage.action(db,run_id,"developer",stage,attempt,"retry_effort",reason)
        return dict(choice,effort=effort)
    usage.action(db,run_id,"developer",stage,attempt,"retry_unchanged",reason)
    return choice


def hold(db,run_id,role,stage,attempt,kind,reason,engine=None,alias=None):
    previous = db.execute("SELECT * FROM studio_usage_holds WHERE run_id=?", (run_id,)).fetchone()
    if previous and (previous["role"],previous["stage"],previous["attempt"],previous["reason"],previous["kind"]) == (
            role, stage, attempt, reason, kind):
        return
    db.execute("""INSERT OR REPLACE INTO studio_usage_holds VALUES(?,?,?,?,?,?,?,?,?,NULL)""",
               (run_id,role,stage,attempt,kind,reason,now(),engine,alias))
    usage.action(db,run_id,role,stage,attempt,kind,reason)


def release(db,run_id):
    previous = db.execute("SELECT * FROM studio_usage_holds WHERE run_id=?", (run_id,)).fetchone()
    if previous and previous["kind"] == "quota_wait":
        usage.action(db,run_id,previous["role"],previous["stage"],previous["attempt"],"resumed",
                     "선택한 계정의 새 공급자 근거 또는 허용한 대체 배정을 확인했습니다.")
        db.execute("DELETE FROM studio_usage_holds WHERE run_id=?", (run_id,))


def admission(db,run_id,stage,attempt,*,record=False):
    """Return a wait reason or a terminal operator stop at a safe stage boundary."""
    spec,engines = assignment(db,run_id)
    if not spec.get("goal_policy"):
        return None
    profile = spec.get("work_profile",{})
    # Local verification can still preserve the changed tree and its receipts.
    # A goal token cap gates the next provider call, not an already earned pytest result.
    stop = budget(db,run_id,profile) if stage_roles(stage,spec) else None
    if stop:
        if record:
            hold(db,run_id,"operator",stage,attempt,"budget_stop",stop[1])
        return "stop",stop
    for role in stage_roles(stage,spec):
        selected,blocked = select(db,run_id,role,profile,engines)
        if selected is not None:
            continue
        reason = reason_for(blocked)
        alias = choices(profile,engines,role)[0].get("account_alias")
        if profile.get("on_limit") == "stop":
            reason = "운영자 설정에 따라 중단했습니다. " + reason.split(" · ")[0]
            if record:
                hold(db,run_id,role,stage,attempt,"quota_stop",reason)
            return "stop",("PROVIDER_LIMIT_STOPPED",reason)
        if record:
            hold(db,run_id,role,stage,attempt,"quota_wait",reason,engines[role],alias)
        return "wait",reason
    if record:
        release(db,run_id)
    return None


def maybe_probe(store,run_id):
    due = store.connection.execute("""SELECT 1 FROM studio_usage_holds WHERE run_id=? AND kind='quota_wait'
        AND (next_probe_at IS NULL OR next_probe_at<=?)""",(run_id,now())).fetchone()
    if due:
        poll_due(store.path,run_id)


def poll_due(path,run_id):
    """Run only fixed provider metadata in the background, without holding a runner lock."""
    with TaskStore(path,existing=True) as store,store._write():
        db = store.connection
        row = db.execute("SELECT * FROM studio_usage_holds WHERE run_id=? AND kind='quota_wait'",(run_id,)).fetchone()
        if row is None or row["next_probe_at"] and row["next_probe_at"] > now():
            return
        spec,engines = assignment(db,run_id)
        role = row["role"]
        binding = choices(spec.get("work_profile",{}),engines,role)[0]
        block = usage.blockers(db,run_id,role,binding)
        if not block:
            return
        due = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
        db.execute("UPDATE studio_usage_holds SET next_probe_at=? WHERE run_id=?",(due,run_id))
        if not (block["account_key"] or block["account_alias"]):
            return
        engine,alias = binding["engine"],binding.get("account_alias")

    def check():
        from .provider_profiles import probe_quota
        evidence = probe_quota(engine)
        if not evidence:
            return
        with TaskStore(path,existing=True) as store,store._write():
            db = store.connection
            for event in evidence:
                if event.get("kind") == "rate_limit":
                    if block["account_key"] and event.get("account_key") != block["account_key"]:
                        continue
                    usage.record_rates(db,run_id,role,engine,alias,"probe-"+uuid4().hex,event,"metadata_probe")
    threading.Thread(target=check,name="office-quota-metadata",daemon=True).start()


class RoomWorkers:
    def __init__(self,path,run_id,adapters,spec,engines):
        self.path = path
        self.run_id = run_id
        self.adapters = adapters
        self.profile = spec.get("work_profile",{})
        self.engines = engines
        self.enforce = bool(spec.get("goal_policy"))

    def bind(self,role,stage,attempt):
        with TaskStore(self.path,existing=True) as store,store._write():
            db = store.connection
            choice, blocked = select(db,self.run_id,role,self.profile,self.engines) if self.enforce else (
                choices(self.profile,self.engines,role)[0],None)
            if choice is None:
                # No durable checkpoint exists inside a compound operation. Preserve its earlier work.
                reason = "진행 중인 단계에서 한도에 도달했습니다. 저장된 작업을 검토하세요. " + reason_for(blocked)
                hold(db,self.run_id,role,stage,attempt,"quota_stop",reason)
                stop = OperatorStop("PROVIDER_LIMIT_STOPPED",reason)
            else:
                stop = None
                if not blocked and role == "developer":
                    choice = retry_choice(db,self.run_id,stage,attempt,choice,self.profile)
                if stage in ("implement","review","reproduce","parallel_implementation"):
                    db.execute("INSERT OR REPLACE INTO studio_usage_roles VALUES(?,?,?,?)",
                               (self.run_id,role,attempt,choice["engine"]))
        if stop:
            raise stop
        return BoundWorker(self,role,stage,attempt,choice,blocked)


class BoundWorker:
    def __init__(self,room,role,stage,attempt,choice,block):
        self.room,self.role,self.stage,self.attempt = room,role,stage,attempt
        self.choice,self.engine,self.block = choice,choice["engine"],block

    def execute(self,prompt,**options):
        room,choice = self.room,self.choice
        with TaskStore(room.path,existing=True) as store,store._write():
            db = store.connection
            stop = budget(db,room.run_id,room.profile) if room.enforce else None
            if stop:
                hold(db,room.run_id,self.role,self.stage,self.attempt,"budget_stop",stop[1])
            else:
                if self.block:
                    old = choices(room.profile,room.engines,self.role)[0]
                    reason = (f'{old["engine"]}/{old.get("model") or "default"}/{old.get("effort") or "default"} → '
                              f'{self.engine}/{choice.get("model") or "default"}/{choice.get("effort") or "default"}'
                              ' · 공급자 한도, 사전 지정 대체 순서')
                    usage.action(db,room.run_id,self.role,self.stage,self.attempt,"fallback",reason)
                ident = usage.begin(db,room.run_id,self.role,self.stage,self.attempt,choice)
        if stop:
            raise OperatorStop(*stop)

        def telemetry(event):
            with TaskStore(room.path,existing=True) as journal,journal._write():
                usage.observe(journal.connection,ident,event)
        if room.profile.get("retry_effort") and self.stage == "implement":
            prompt += ("\n다른 가설이 있고 추가 검증이 필요하면 '다음 가설: ' 한 줄에 변경 파일 또는 실패 검사와 "
                       "다음에 확인할 원인을 명시하세요. 환경 문제나 제품 질문은 해당 절차를 사용하세요.")
        result = room.adapters[self.engine].execute(prompt,**options, model=choice.get("model"),
                        effort=choice.get("effort"), telemetry=telemetry)
        with TaskStore(room.path,existing=True) as store:
            if getattr(result, "preflight_blocked", False):
                # No model turn was started. An explicit token cap can safely survive this declined dispatch.
                store.connection.execute("""UPDATE studio_usage_calls SET total_tokens=0,source='preflight_not_started',
                    observed_at=? WHERE id=? AND total_tokens IS NULL""", (now(),ident))
            store.connection.execute("UPDATE studio_usage_calls SET finished_at=? WHERE id=?",(now(),ident))
        if not (room.enforce and getattr(result, "preflight_blocked", False)):
            return result
        with TaskStore(room.path, existing=True, readonly=True) as store:
            confirmed = usage.blockers(store.connection,room.run_id,self.role,choice)
        if not confirmed or self.stage in ("meeting","parallel_implementation"):
            reason = "공급자가 호출 전 한도를 보고했습니다. 현재 회의 또는 병렬 작업에서 저장된 근거를 검토하세요."
            with TaskStore(room.path,existing=True) as store,store._write():
                hold(store.connection,room.run_id,self.role,self.stage,self.attempt,"quota_stop",reason)
            raise OperatorStop("PROVIDER_LIMIT_STOPPED",reason)
        return result
