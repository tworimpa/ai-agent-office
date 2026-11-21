"""Host-scoped work policy, character evidence, and journal-only usage reads."""
import json
import platform
import re

from . import character_profile, work_profiles
from .models import OfficeError, validate_id
from .provider_profiles import host_capabilities
from .store import TaskStore, now


def route(method, path):
    match = re.fullmatch(r"/rooms/([a-f0-9]{32})/(work-policy|models|visit|usage/([a-f0-9]{32})|characters/(developer|reviewer|coordinator))", path)
    if match and (method == "GET" and match[2] != "visit" or method == "POST" and match[2] in ("work-policy", "models", "visit")):
        return match[1], match[2].split("/")[0], match[3] or match[4]
    return None


def _usage(db, workspace, rid):
    from . import goal_recovery, goal_usage

    row = db.execute("""SELECT r.attempts,a.assignment_json FROM runs r JOIN studio_runs s ON r.run_id=s.id
        LEFT JOIN studio_room_assignments a ON a.run_id=s.id WHERE s.workspace_id=? AND s.id=? AND r.host_name=?""",
                     (workspace, rid, platform.node())).fetchone()
    if row is None:
        raise OfficeError("NOT_FOUND")
    assignment = json.loads(row["assignment_json"]) if row["assignment_json"] else {}
    profile = assignment.get("work_profile")
    limits = assignment.get("goal_policy", {})
    clock = goal_recovery.timing(db,rid) if limits else {}
    return dict(run_id=rid,profile=profile,usage=goal_usage.describe(db, rid),
                budget=dict(active_seconds=limits.get("active_seconds"),max_attempts=limits.get("max_attempts"),
                            active_used=clock.get("active_seconds"),attempts_used=row["attempts"],
                            token_budget=(profile or {}).get("token_budget")))


def handle(server, workspace, action, target, data):
    server.catalog.get(workspace)
    catalog = server.rooms.models
    if action == "models":
        if data is not None and data != {}:
            raise ValueError("모델 목록 새로고침 요청을 확인하세요.")
        info = catalog.read() if data is None else catalog.refresh()
        return dict(workspace_id=workspace,catalog=info,
                    capabilities=host_capabilities(server.engines.snapshot()["engines"],info))
    with TaskStore(server.db_path, existing=True, readonly=data is None) as store:
        db = store.connection
        if data is None:
            db.execute("BEGIN")
        if action == "visit":
            if not isinstance(data, dict) or set(data) != {"id"}:
                raise ValueError("방문 기록 ID를 확인하세요.")
            ident = validate_id(data["id"])
            with store._write():
                db.execute("""INSERT OR IGNORE INTO studio_workspace_visits(workspace_id,id,visited_at,previous_visit)
                    SELECT ?,?,?,MAX(visited_at) FROM studio_workspace_visits WHERE workspace_id=?""",
                           (workspace,ident,now(),workspace))
                receipt = db.execute("SELECT * FROM studio_workspace_visits WHERE workspace_id=? AND id=?",
                                     (workspace, ident)).fetchone()
            return dict(receipt)
        if action == "work-policy":
            if data is None:
                policy, _ = work_profiles.stored(db, workspace)
            else:
                policy = work_profiles.merge(work_profiles.defaults(), data)
                roles = server.rooms._settings(db, workspace)
                engines = {r: roles[r]["engine"] for r in work_profiles.ROLES}
                work_profiles.validate_targets(catalog, policy, engines, server.engines.snapshot()["engines"])
                with store._write():
                    latest = server.rooms._settings(db, workspace)
                    if any(latest[r]["engine"] != engines[r] for r in engines):
                        raise OfficeError("REQUEST_CONFLICT")
                    work_profiles.save(db, workspace, policy, engines)
            return dict(workspace_id=workspace,policy=policy)
        if action == "characters":
            name = "조정자" if target == "coordinator" else server.rooms._settings(db,workspace)[target]["name"]
            return dict(workspace_id=workspace,character=character_profile.describe(db,workspace,name,target))
        return dict(workspace_id=workspace, **_usage(db, workspace, target))


def check_reply(binding, status, data, method, payload):
    from .studio_ssh import RemoteUnavailable

    workspace, action, target = binding
    if status != 200 or not isinstance(data,dict) or data.get("workspace_id") != workspace:
        raise RemoteUnavailable()
    if action == "visit":
        if data.get("id") != payload.get("id") or not isinstance(data.get("visited_at"),str) or (
                data.get("previous_visit") is not None and not isinstance(data["previous_visit"],str)):
            raise RemoteUnavailable()
    elif action == "work-policy":
        policy = work_profiles.merge(work_profiles.defaults(), data.get("policy"))
        if data["policy"] != policy or method == "POST" and policy != work_profiles.merge(work_profiles.defaults(),payload):
            raise RemoteUnavailable()
    elif action == "usage":
        if data.get("run_id") != target or not isinstance(data.get("budget"),dict) or not isinstance(data.get("usage"),dict):
            raise RemoteUnavailable()
        if not all(isinstance(data["usage"].get(k),list) for k in ("calls","rate_limits","decisions")):
            raise RemoteUnavailable()
    elif action == "characters":
        character = data.get("character")
        if not isinstance(character,dict) or character.get("role") != target or not isinstance(character.get("axes"), list):
            raise RemoteUnavailable()
        if [row.get("id") for row in character["axes"] if isinstance(row,dict)] != [axis for axis,_ in character_profile.AXES]:
            raise RemoteUnavailable()
    else:
        catalog = data.get("catalog")
        if (not isinstance(catalog,dict) or catalog.get("engine") != "codex"
                or catalog.get("status") not in ("unprobed","available","unavailable")
                or not isinstance(catalog.get("models"),list) or not isinstance(data.get("capabilities"),dict)):
            raise RemoteUnavailable()
