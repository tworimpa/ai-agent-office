"""Workspace defaults and the immutable execution profile attached to an assignment."""
import json
import re

from .engines import SUPPORTED_ENGINES
from .events import snapshot_request
from .store import encode

ROLES = ("developer", "reviewer")
MODES = {"auto", "quick", "balanced", "deep"}
LABELS = {"quick": "빠른 확인", "balanced": "균형 개발", "deep": "심층 검토"}


def defaults():
    return {"mode": "auto", "roles": {r: {"model": None, "effort": None, "account_alias": None} for r in ROLES},
            "token_budget": None, "on_limit": "wait", "retry_deep": False, "fallbacks": {r: [] for r in ROLES}}


def schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS studio_workspace_work_profiles(
        workspace_id TEXT PRIMARY KEY, policy TEXT NOT NULL, role_engines TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_workspace_visits(
        workspace_id TEXT NOT NULL, id TEXT NOT NULL, visited_at TEXT NOT NULL, previous_visit TEXT,
        PRIMARY KEY(workspace_id,id))""")
    db.execute("CREATE INDEX IF NOT EXISTS workspace_recent_visits ON studio_workspace_visits(workspace_id,visited_at)")


def _object(value, fields):
    if not isinstance(value, dict) or value.keys() - fields:
        raise ValueError("작업 방식의 설정 항목을 확인하세요.")
    return value


def _role(value, fallback=False):
    fields = {"model", "effort", "account_alias"}
    p = _object(value, fields | ({"engine"} if fallback else set()))
    if fallback and p.get("engine") not in SUPPORTED_ENGINES:
        raise ValueError("전환을 위임할 엔진을 확인하세요.")
    for key in fields.intersection(p):
        val = p[key]
        if val is None:
            continue
        pattern = r"[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,127}" if key == "model" else r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}"
        if not isinstance(val, str) or not re.fullmatch(pattern, val) or snapshot_request(val) != val:
            raise ValueError("모델·effort·계정 별칭에는 비밀 값 없이 식별자를 입력하세요.")
    return dict(p)


def merge(base, override):
    """A missing key inherits; an explicit null restores that field to CLI default."""
    patch = _object(override, defaults().keys())
    result = {**base, **patch, "roles": {}, "fallbacks": {}}
    if not isinstance(result["mode"], str) or result["mode"] not in MODES:
        raise ValueError("지원되는 작업 방식을 선택하세요.")
    if result["on_limit"] not in ("wait", "stop"):
        raise ValueError("한도 도달 시 행동을 확인하세요.")
    if type(result["retry_deep"]) is not bool:
        raise ValueError("재작업 시 심층 추론 위임 여부를 확인하세요.")
    limit = result["token_budget"]
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 10**12):
        raise ValueError("토큰 예산은 양의 정수로 입력하세요.")
    profiles = _object(patch.get("roles", {}), set(ROLES))
    switches = _object(patch.get("fallbacks", {}), set(ROLES))
    for role in ROLES:
        result["roles"][role] = {**base["roles"][role], **_role(profiles.get(role, {}))}
        rows = switches.get(role, base["fallbacks"][role])
        if not isinstance(rows, list) or len(rows) > 4:
            raise ValueError("담당자별 대체 조합은 네 개까지 위임할 수 있습니다.")
        chain = [{**defaults()["roles"][role], **_role(row, True)} for row in rows]
        if len(set(map(encode, chain))) != len(chain):
            raise ValueError("동일한 대체 조합이 반복됩니다.")
        result["fallbacks"][role] = chain
    return result


def stored(db, workspace):
    row = db.execute("SELECT policy,role_engines FROM studio_workspace_work_profiles WHERE workspace_id=?",
                     (workspace,)).fetchone()
    return (merge(defaults(), json.loads(row[0])), json.loads(row[1])) if row else (defaults(), {})


def _validate(catalog, engine, pref):
    if pref["model"] or pref["effort"]:
        if engine == "codex":
            catalog.refresh(if_stale=True)
        catalog.validate(engine, pref["model"], pref["effort"])


def validate_targets(catalog, profile, engines, host_entries, active_roles=ROLES):
    from .provider_profiles import host_capabilities
    capabilities = host_capabilities(host_entries,catalog.read())
    by_engine = {row["engine"]: row for row in host_entries}
    aliases = {}
    for role in active_roles:
        choices = [dict(engine=engines[role], **profile["roles"][role]), *profile["fallbacks"][role]]
        for fallback in profile["fallbacks"][role]:
            host = by_engine.get(fallback["engine"], {})
            required = "review" if role == "reviewer" else "edit"
            if (not host.get("headless_support") or host.get("probe_status") != "ok"
                    or host.get("auth_status") == "not_authenticated"
                    or host.get("capabilities", {}).get(required, {}).get("status") == "failed"):
                raise ValueError("이 호스트에서 역할을 실행할 수 있는 대체 엔진을 선택하세요.")
        for choice in choices:
            engine, alias = choice["engine"], choice["account_alias"]
            if alias and aliases.setdefault(engine, alias) != alias:
                raise ValueError("한 호스트의 같은 CLI를 다른 계정으로 전환할 수 없습니다. 동일한 계정 별칭을 사용하세요.")
            if engine != "codex" and any(choice[k] and capabilities[engine][k] != "supported" for k in ("model","effort")):
                raise ValueError(f"이 호스트에서 {engine}의 모델 또는 추론 강도 옵션을 확인할 수 없습니다.")
            if engine != "codex" and choice["effort"] and choice["effort"] not in by_engine[engine].get("profile_efforts",[]):
                raise ValueError(f"이 호스트의 {engine}에서 확인된 추론 강도를 선택하세요.")
            _validate(catalog, engine, choice)


def save(db, workspace, profile, engines):
    db.execute("""INSERT INTO studio_workspace_work_profiles VALUES(?,?,?) ON CONFLICT(workspace_id)
        DO UPDATE SET policy=excluded.policy,role_engines=excluded.role_engines""",
               (workspace, encode(profile), encode(engines)))


def _preset(model_list, model, mode):
    selected = next((row for row in model_list if row["id"] == model), None) if model else next(
        (row for row in model_list if row["is_default"]), None)
    if not selected:
        return model, None
    order = {"quick": ("low", "minimal", "medium"), "balanced": ("medium",), "deep": ("high", "xhigh", "medium")}
    effort = next((e for e in order[mode] if e in selected["efforts"]), selected["default_effort"])
    return selected["id"], effort


def effective_mode(mode, edits):
    return ("balanced" if edits else "quick") if mode == "auto" else mode


def resolve(saved, bound_engines, overrides, roles, catalog, host_entries, *, edits, goal, reproduce, active_roles=ROLES):
    base = merge(defaults(), saved)
    for role in ROLES:
        if bound_engines.get(role, roles[role]["engine"]) != roles[role]["engine"]:
            # A model or billing alias for one CLI must not follow an automatic switch to another CLI.
            base["roles"][role] = defaults()["roles"][role]
    profile = merge(base, overrides)
    mode = effective_mode(profile["mode"], edits)
    if not goal:
        if (overrides.get("token_budget") is not None or overrides.get("on_limit") == "stop" or overrides.get("retry_deep")
                or any(overrides.get("fallbacks", {}).values())):
            raise ValueError("토큰 예산과 한도 대기·전환 정책은 장시간 목표에서 설정하세요.")
        profile.update(token_budget=None, on_limit="wait", retry_deep=False, fallbacks={r: [] for r in ROLES})
    validate_targets(catalog, profile, {r: roles[r]["engine"] for r in ROLES},host_entries,active_roles)
    cached = catalog.read()
    if any(roles[r]["engine"] == "codex" for r in active_roles):
        cached = catalog.refresh(if_stale=True)
    for role in active_roles:
        pref = profile["roles"][role]
        inferred = "deep" if role == "reviewer" and goal and reproduce and profile["mode"] == "auto" else mode
        if roles[role]["engine"] == "codex" and cached["status"] == "available" and not pref["effort"]:
            pref["model"], pref["effort"] = _preset(cached["models"], pref["model"], inferred)
        elif roles[role]["engine"] in ("claude","antigravity") and not pref["effort"]:
            host = next((r for r in host_entries if r["engine"] == roles[role]["engine"]), {})
            target = {"quick":"low","balanced":"medium","deep":"high"}[inferred]
            if target in host.get("profile_efforts",[]):
                pref["effort"] = target
    if profile["retry_deep"] and roles["developer"]["engine"] == "codex":
        developer = profile["roles"]["developer"]
        _, next_effort = _preset(cached["models"], developer["model"], "deep")
        if developer["effort"] in ("minimal","low","medium") and next_effort in ("high","xhigh"):
            profile["retry_effort"] = next_effort
    basis = "원본 실패를 재현하고 독립 리뷰를 거칩니다." if reproduce and goal else (
        "변경과 검증을 담당자별로 진행합니다." if edits else "읽기 전용으로 범위를 확인합니다.")
    profile["reason"] = f"{LABELS[mode]} · {basis}"
    if not goal and (saved["token_budget"] is not None or any(saved["fallbacks"].values()) or saved["on_limit"] == "stop"):
        profile["reason"] += " Workspace 예산·한도 정책은 장시간 목표에 적용됩니다."
    return profile
