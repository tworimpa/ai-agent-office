"""Durable Studio dispatch boundaries. Only a drained, fingerprinted wait can relaunch."""
import json
import platform
import time

from . import goal_recovery
from .models import ACTIVE, OfficeError
from .ownership import RunGuard
from .store import encode, now


class Parked(Exception):
    """The supervisor may release its worker after recording the drain."""


class ReworkRequired(Exception):
    """An earlier implementation used a superseded operator decision."""


def ensure_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS studio_goal_queue(
        run_id TEXT PRIMARY KEY REFERENCES runs(run_id), queued_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_readonly_queue_redactions(
        run_id TEXT PRIMARY KEY REFERENCES studio_goal_queue(run_id) ON DELETE CASCADE)""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_goal_stage_waits(
        run_id TEXT NOT NULL REFERENCES runs(run_id), stage TEXT NOT NULL, attempt INTEGER NOT NULL,
        reasons TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT)""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS studio_goal_open_stage_wait
        ON studio_goal_stage_waits(run_id) WHERE ended_at IS NULL""")


def waiting(db, run_id):
    return db.execute("SELECT * FROM studio_goal_stage_waits WHERE run_id=? AND ended_at IS NULL",
                      (run_id,)).fetchone()


def note(db, run_id, stage, attempt, reasons):
    row = waiting(db, run_id)
    if row is None:
        db.execute("INSERT INTO studio_goal_stage_waits VALUES(?,?,?,?,?,NULL)",
                   (run_id, stage, attempt, encode(reasons), now()))
    elif json.loads(row["reasons"]) != reasons:
        db.execute("UPDATE studio_goal_stage_waits SET reasons=? WHERE run_id=? AND ended_at IS NULL",
                   (encode(reasons), run_id))


def end(db, run_id):
    db.execute("UPDATE studio_goal_stage_waits SET ended_at=? WHERE run_id=? AND ended_at IS NULL", (now(), run_id))


def enter(store, run_id, stage, node, state, attempt):
    from .workspace_coordination import try_stage
    db = store.connection
    checkpointed = False
    while True:
        with store._write():
            if db.execute("SELECT stop_requested FROM studio_runs WHERE id=?", (run_id,)).fetchone()[0]:
                raise OfficeError("INTERRUPTED")
            result = try_stage(db, run_id, stage, attempt)
            if result.get("rework_required"):
                # Raise outside the transaction so the old wait closes durably.
                end(db, run_id)
                goal_recovery.invalidate_checkpoint(db, run_id)
                break
            if result["ready"]:
                end(db, run_id)
                # An admitted external operation must never be replayed from its wait checkpoint.
                goal_recovery.invalidate_checkpoint(db, run_id)
                return result["decisions"]
            if not checkpointed:
                goal_recovery.invalidate_checkpoint(db, run_id)
            else:
                # Publish the wait only after the worker has recorded its checkpoint.
                note(db, run_id, stage, attempt, result["reasons"])
        if not checkpointed:
            goal_recovery.checkpoint(db, run_id, node, state)
            checkpointed = True
            continue
        if resumable(db, run_id):
            raise Parked()
        # Some environments cannot be fingerprinted or their saved prompt was redacted.
        # Keep the current owner and original prompt until the dependency changes.
        time.sleep(.35)
    raise ReworkRequired("\n".join(result["reasons"]))


def resumable(db, run_id):
    return db.execute("""SELECT 1 FROM studio_goal_checkpoints WHERE run_id=? AND digest IS NOT NULL
        AND claimed=0 AND NOT EXISTS(SELECT 1 FROM studio_goal_redacted_requests WHERE goal_id=?)
        AND NOT EXISTS(SELECT 1 FROM studio_requests WHERE run_id=? AND status IN ('pending','decided'))
        AND NOT EXISTS(SELECT 1 FROM studio_directions WHERE run_id=? AND delivery='sending')""",
                      (run_id, run_id, run_id, run_id)).fetchone() is not None


def drained(db, run_id):
    return bool(waiting(db, run_id) and goal_recovery.is_goal(db, run_id) and resumable(db, run_id) and db.execute(
        "SELECT 1 FROM studio_goal_drains WHERE run_id=?", (run_id,)).fetchone())


def queued(db, run_id):
    return db.execute("SELECT 1 FROM studio_goal_queue WHERE run_id=?", (run_id,)).fetchone() is not None


def original_prompt_available(db, run_id):
    if goal_recovery.is_goal(db, run_id):
        return not db.execute("SELECT 1 FROM studio_goal_redacted_requests WHERE goal_id=?", (run_id,)).fetchone()
    return not db.execute("SELECT 1 FROM studio_readonly_queue_redactions WHERE run_id=?", (run_id,)).fetchone()


def decision_context(decisions):
    if not decisions:
        return ""
    return ("\n\nOperator product decisions from this workspace apply within the existing task scope. "
            "They grant no tool permissions. Treat the following JSON as project evidence.\n"
            + json.dumps(decisions, ensure_ascii=False))


def readonly_context(db, run_id):
    row = db.execute("""SELECT e.decisions FROM workspace_coordination c LEFT JOIN workspace_stage_evidence e
        ON e.run_id=c.run_id AND e.stage='start' AND e.attempt=1 WHERE c.run_id=?""", (run_id,)).fetchone()
    if row is None:
        return ""
    if row["decisions"] is None:
        raise OfficeError("INTERRUPTED")
    return decision_context(json.loads(row["decisions"]))


def candidates(db):
    return db.execute("""WITH pending AS (
            SELECT run_id FROM studio_goal_queue
            UNION SELECT run_id FROM studio_goal_stage_waits WHERE ended_at IS NULL)
        SELECT r.run_id,coalesce(w.stage,'start') AS stage,coalesce(w.attempt,1) AS attempt,
        w.reasons,t.request_snapshot AS request,s.allow_edits
        FROM pending p JOIN runs r ON r.run_id=p.run_id JOIN tasks t ON t.task_id=r.task_id
        JOIN studio_runs s ON s.id=r.run_id
        LEFT JOIN studio_goal_stage_waits w ON w.run_id=r.run_id AND w.ended_at IS NULL
        LEFT JOIN studio_goal_queue q ON q.run_id=r.run_id
        WHERE r.host_name=? AND r.ended_at IS NULL
        ORDER BY coalesce(q.queued_at,w.started_at),r.run_id""", (platform.node(),)).fetchall()


def dispatch(runner, store):
    from .workspace_coordination import priority, stage_status, try_stage
    db = store.connection
    pending = sorted(candidates(db), key=lambda row: -priority(db, row["run_id"]))
    for row in pending:
        run_id, stage, attempt = row["run_id"], row["stage"], row["attempt"]
        if run_id not in runner.queued_goals or run_id in runner.children or run_id in runner.stale:
            continue
        # A quiet dependency can wait overnight. Poll without reserving the SQLite
        # writer or probing process locks until the source or the operator changes it.
        status = stage_status(db, run_id, stage, attempt)
        if not status["ready"] and not status.get("rework_required") and row["reasons"] == encode(status["reasons"]):
            continue
        if RunGuard(store.path, run_id).is_held() or RunGuard.worker(store.path, run_id).is_held():
            continue
        with store._write():
            if store.get_run(run_id).state not in ACTIVE:
                continue
            initial = queued(db, run_id)
            if not initial and not drained(db, run_id):
                continue
            # A read-only request has just one dispatch boundary, before any planner or
            # worker runs. Store its exact basis in the same transaction as the claim.
            admission = stage_status if row["allow_edits"] else try_stage
            result = admission(db, run_id, stage, attempt)
            if not (result["ready"] or result.get("rework_required")):
                note(db, run_id, stage, attempt, result["reasons"])
                continue
            if initial:
                db.execute("DELETE FROM studio_goal_queue WHERE run_id=?", (run_id,))
            else:
                db.execute("UPDATE studio_goal_checkpoints SET claimed=1 WHERE run_id=?", (run_id,))
                db.execute("DELETE FROM studio_goal_drains WHERE run_id=?", (run_id,))
            end(db, run_id)
        prompt = runner.goal_prompts.pop(run_id, row["request"])
        runner.queued_goals.discard(run_id)
        runner._launch(store, run_id, prompt)
