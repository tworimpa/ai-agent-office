"""Read-only Studio office projection, scoped to the host that owns the journal."""
import json
import platform
from functools import cache

from .models import MAX_SQLITE_INT
from .store import TaskStore, now
from .studio_inbox import ROLE_STATE, holder_alive
from .technical_choices import CHOICE_MARKER, public_prompt


PAGE_SIZE = 24
RECENT_RUNS = 8
REQUEST_PREVIEWS = 3
TASK_LIMIT = 320
PREVIEW_LIMIT = 240
ACTIVITIES = frozenset({"queued", "working", "testing", "waiting", "done", "failed", "stopped",
                        "stopping", "skipped", "recovery", "checking"})

# The CASE guards the OS probe even if SQLite reorders joins or predicates.
# Only UUIDs are concatenated; arbitrary prompts stay in parameterized queries.
_PAGE = """
WITH live AS (
    SELECT q.run_id, q.id, coalesce(q.decision_type,q.kind) AS kind, q.created_at,
           (q.kind='question' AND q.decision_type IS NULL
            AND ((p.run_id IS NOT NULL AND p.revoked_at IS NULL) OR (cp.run_id IS NOT NULL AND cp.revoked_at IS NULL))
            AND c.request_id IS NOT NULL AND c.reason IS NULL AND c.evidence IS NULL) AS delegated
    FROM studio_requests q
    JOIN studio_interactive_runs i ON i.id=q.run_id
    JOIN studio_runs s ON s.id=q.run_id
    JOIN runs r ON r.run_id=q.run_id
    JOIN tasks t ON t.task_id=r.task_id
    LEFT JOIN studio_away_checks c ON c.request_id=q.id
    LEFT JOIN studio_away_policies p ON p.run_id=q.run_id
    LEFT JOIN studio_away_choice_policies cp ON cp.run_id=q.run_id
    WHERE q.status='pending' AND CASE WHEN r.host_name=:host AND t.current_run_id=q.run_id
      AND r.ended_at IS NULL AND NOT s.stop_requested AND s.proposed_state IS NULL
      AND q.attempt=r.attempts
      AND ((q.role='developer' AND r.state=:dev_state AND q.engine=t.developer_engine)
        OR (q.role='reviewer' AND r.state=:review_state AND q.engine=t.reviewer_engine))
      THEN holder_alive(q.holder) ELSE 0 END
), demand AS (
    SELECT run_id, sum(kind='question' AND NOT delegated) AS questions,
           sum(kind='permission') AS permissions, sum(kind='product_decision') AS product_decisions
    FROM live GROUP BY run_id
), recent AS (
    SELECT r.run_id, r.ended_at AS updated_at
    FROM runs r JOIN studio_runs s ON s.id=r.run_id
    WHERE r.host_name=:host AND r.ended_at IS NOT NULL
    ORDER BY r.ended_at DESC, r.run_id LIMIT :recent
), eligible AS (
    SELECT r.run_id, t.updated_at, 1 AS active,
           coalesce(d.questions,0) AS questions, coalesce(d.permissions,0) AS permissions,
           coalesce(d.product_decisions,0) AS product_decisions
    FROM runs r JOIN studio_runs s ON s.id=r.run_id JOIN tasks t ON t.task_id=r.task_id
    LEFT JOIN demand d ON d.run_id=r.run_id
    WHERE r.host_name=:host AND r.ended_at IS NULL
    UNION ALL SELECT run_id, updated_at, 0, 0, 0, 0 FROM recent
), totals AS (
    SELECT count(*) AS total_runs, coalesce(sum(active),0) AS active_runs,
           coalesce(sum(questions),0) AS total_questions, coalesce(sum(permissions),0) AS total_permissions,
           coalesce(sum(product_decisions),0) AS total_product_decisions
    FROM eligible
), page AS (
    SELECT *, (questions+permissions+product_decisions) AS requests,
           (questions+permissions+product_decisions>0) AS needs_human FROM eligible
    ORDER BY needs_human DESC, active DESC, updated_at DESC, run_id LIMIT :size OFFSET :offset
)
SELECT totals.*, page.*, r.state, r.attempts, s.stop_requested, s.proposed_state,
       s.allow_edits, t.developer_engine, t.reviewer_engine,
       substr(t.request_snapshot,1,:task_chars+1) AS task,
       parent.id AS parent_id, substr(parent.task,1,:task_chars+1) AS parent_task,
       (SELECT group_concat(id) FROM (
            SELECT l.id FROM live l
            WHERE l.run_id=page.run_id AND NOT l.delegated
            ORDER BY l.created_at, l.id LIMIT :previews
       )) AS preview_ids,
       (SELECT group_concat(id) FROM live WHERE run_id=page.run_id AND delegated) AS delegated_ids
FROM totals LEFT JOIN page ON true LEFT JOIN runs r ON r.run_id=page.run_id
LEFT JOIN studio_runs s ON s.id=page.run_id LEFT JOIN tasks t ON t.task_id=r.task_id
LEFT JOIN studio_team_children child ON child.id=page.run_id
LEFT JOIN studio_teams parent ON parent.id=child.parent_id AND parent.host_name=:host
ORDER BY page.needs_human DESC, page.active DESC, page.updated_at DESC, page.run_id
"""


def _snippet(text, limit):
    return text if text is None or len(text) <= limit else text[:limit-1] + "…"


def _ids(value):
    return value.split(",") if value else []


def _request_previews(db, rows):
    ids = [request_id for row in rows for request_id in _ids(row["preview_ids"])]
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    result = db.execute(f"""SELECT id,coalesce(decision_type,kind) AS kind,
                            CASE WHEN kind='question' AND decision_type IS NULL AND instr(prompt,?)=1 THEN prompt
                                 ELSE substr(prompt,1,?) END AS prompt
                            FROM studio_requests WHERE id IN ({marks})""",
                        (CHOICE_MARKER + "\n", PREVIEW_LIMIT + 1, *ids))
    return {row["id"]: dict(row, prompt=_snippet(public_prompt(row["prompt"]) if row["kind"] == "question"
                                                else row["prompt"], PREVIEW_LIMIT)) for row in result}


def _latest_stages(db, run_ids):
    marks = ",".join("?" for _ in run_ids)
    stages = {run_id: {} for run_id in run_ids}
    rows = db.execute(f"""WITH latest AS (
        SELECT run_id,role,max(seq) AS seq FROM events WHERE run_id IN ({marks})
        AND role IN ('developer','tester','reviewer')
        AND event_type IN ('STAGE_STARTED','STAGE_COMPLETED','STAGE_FAILED','RETRY_SCHEDULED')
        GROUP BY run_id,role)
        SELECT e.run_id,e.role,e.event_type,e.payload_json,r.attempts
        FROM latest JOIN events e USING(run_id,seq) JOIN runs r ON r.run_id=e.run_id""", run_ids)
    for row in rows:
        if json.loads(row["payload_json"])["attempt"] == row["attempts"]:
            stages[row["run_id"]][row["role"]] = row["event_type"]
    return stages


def _activity(run, role, stages):
    stage = stages.get(role)
    if run["active"] and not run["stop_requested"] and not run["proposed_state"]:
        if run["state"] == ROLE_STATE[role]:
            if run["requests"]:
                return "waiting"
            if run["delegated_ids"]:
                return "checking"
        if role == "developer" and run["state"] == "TESTING" and stages.get("tester") == "STAGE_STARTED":
            return "testing"
        if run["state"] == "RUNNING" and "RETRY_SCHEDULED" in stages.values():
            return "queued"
    if stage == "STAGE_FAILED":
        return "failed"
    if stage == "STAGE_COMPLETED":
        return "done"
    if stage == "STAGE_STARTED":
        if run["active"] and run["stop_requested"]:
            return "stopping"
        state = run["proposed_state"] or run["state"]
        if state == "CANCELED":
            return "stopped"
        if state == "RECOVERY_REQUIRED":
            return "recovery"
        if state == "FAILED":
            return "failed"
        if state == ROLE_STATE[role]:
            return "working"
        return "recovery"
    if run["active"] and not run["stop_requested"] and not run["proposed_state"]:
        return "queued"
    return "skipped"


def _away_roles(db, run_ids, delegated):
    # One execution may consult several times in one attempt. A current call
    # takes precedence over a later timestamp on a finished question.
    marks = ",".join("?" for _ in run_ids)
    rows = db.execute(f"""WITH questions AS (
        SELECT q.id,q.run_id,q.decided_at FROM studio_requests q JOIN runs r ON r.run_id=q.run_id
        WHERE q.run_id IN ({marks}) AND q.attempt=r.attempts AND q.kind='question' AND q.decision_type IS NULL)
        SELECT q.run_id,a.role,a.engine,a.status,a.updated_at,q.id
        FROM questions q JOIN studio_away_role_stages a ON a.request_id=q.id
        JOIN studio_away_checks c ON c.request_id=q.id WHERE c.evidence IS NULL
        UNION ALL
        SELECT q.run_id,legacy.role,CASE legacy.role WHEN 'specialist' THEN c.specialist_engine ELSE c.verifier_engine END,
            'COMPLETED',q.decided_at,q.id
        FROM questions q JOIN studio_away_checks c ON c.request_id=q.id
        CROSS JOIN (SELECT 'specialist' AS role UNION ALL SELECT 'verifier') AS legacy
        WHERE c.evidence IS NOT NULL AND c.specialist_engine IS NOT NULL AND c.verifier_engine IS NOT NULL
        ORDER BY updated_at DESC,id DESC""", run_ids)
    by_run = {run_id: {} for run_id in run_ids}
    for row in rows:
        activity = {"RUNNING": "working" if row["id"] in delegated else "stopped",
                    "FAILED": "failed", "INTERRUPTED": "stopped", "COMPLETED": "done"}[row["status"]]
        role = row["role"]
        roles = by_run[row["run_id"]]
        if role not in roles or (activity == "working" and roles[role][1] != "working"):
            roles[role] = row["engine"], activity, row["updated_at"]
    return by_run


def _cards(run, stages, away_roles, previews):
    common = dict(run_id=run["run_id"], task=_snippet(run["task"], TASK_LIMIT),
                  parent_id=run["parent_id"], parent_task=_snippet(run["parent_task"], TASK_LIMIT),
                  run_state="STOP_REQUESTED" if run["active"] and run["stop_requested"] else run["state"])
    roles = ("developer", "reviewer") if run["allow_edits"] else ("developer",)
    for role in roles:
        activity = _activity(run, role, stages)
        requests = [previews[key] for key in _ids(run["preview_ids"])] if activity == "waiting" else []
        yield dict(common, role=role, engine=run[role+"_engine"], activity=activity,
                   requests=requests, request_count=run["requests"] if requests else 0,
                   updated_at=run["updated_at"])
    for role, (engine, activity, stamp) in away_roles.items():
        yield dict(common, role=role, engine=engine, activity=activity,
                   requests=[], request_count=0, updated_at=stamp)


def office_snapshot(db_path, offset=0):
    if type(offset) is not int or not 0 <= offset <= MAX_SQLITE_INT:
        raise ValueError("Invalid office offset")
    with TaskStore(db_path, readonly=True) as store:
        db = store.connection
        # A holder can own several requests. Keep one lock observation per
        # currently bound channel for the duration of this read transaction.
        db.create_function("holder_alive", 1, cache(lambda holder: holder_alive(store.path, holder)))
        db.execute("BEGIN")
        rows = db.execute(_PAGE, dict(host=platform.node(), recent=RECENT_RUNS, size=PAGE_SIZE,
                                     offset=offset, task_chars=TASK_LIMIT,
                                     previews=REQUEST_PREVIEWS, dev_state=ROLE_STATE["developer"],
                                     review_state=ROLE_STATE["reviewer"])).fetchall()
        total = rows[0]["total_runs"]
        counts = dict(active_runs=rows[0]["active_runs"], questions=rows[0]["total_questions"],
                      permissions=rows[0]["total_permissions"])
        if rows[0]["total_product_decisions"]:
            counts["product_decisions"] = rows[0]["total_product_decisions"]
        run_ids = [run["run_id"] for run in rows if run["run_id"]]
        agents = []
        if run_ids:
            delegated = {request_id for run in rows for request_id in _ids(run["delegated_ids"])}
            stages = _latest_stages(db, run_ids)
            away_roles = _away_roles(db, run_ids, delegated)
            previews = _request_previews(db, rows)
            for run in rows:
                run_id = run["run_id"]
                agents.extend(_cards(run, stages[run_id], away_roles[run_id], previews))
        next_offset = offset + PAGE_SIZE if offset + PAGE_SIZE < total else None
        return dict(generated_at=now(), offset=offset, next_offset=next_offset,
                    total_runs=total, counts=counts, agents=agents)
