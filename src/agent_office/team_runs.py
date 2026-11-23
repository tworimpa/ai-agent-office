"""Durable Studio parent requests and their ordered, independently owned runs."""
import json
import os
import platform

from .engines import SUPPORTED_ENGINES
from .events import snapshot_request
from .models import ACTIVE, OfficeError
from .store import encode, now


def active_slots():
    value = os.environ.get("AGENT_OFFICE_TEAM_SLOTS", "2")
    if value not in {"1", "2", "3", "4", "5", "6"}:
        raise ValueError("AGENT_OFFICE_TEAM_SLOTS must be an integer from 1 to 6")
    return int(value)


def ensure_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS studio_teams(
        id TEXT PRIMARY KEY, request_hash TEXT NOT NULL, workspace_id TEXT NOT NULL, task TEXT NOT NULL,
        indices TEXT NOT NULL, folders TEXT NOT NULL, allow_edits INTEGER NOT NULL,
        developer_engine TEXT NOT NULL, reviewer_engine TEXT NOT NULL, use_inbox INTEGER NOT NULL,
        delegation TEXT, base_revision TEXT NOT NULL, host_name TEXT NOT NULL,
        created_at TEXT NOT NULL, stop_requested INTEGER NOT NULL DEFAULT 0)""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_team_children(
        id TEXT PRIMARY KEY REFERENCES studio_runs(id), parent_id TEXT NOT NULL REFERENCES studio_teams(id),
        position INTEGER NOT NULL, indices TEXT NOT NULL, UNIQUE(parent_id,position))""")
    # Plain CLI callers also write to runs. Once a parent owns an ID, they
    # must not accidentally create a second, differently addressable record.
    db.execute("""CREATE TRIGGER IF NOT EXISTS reserve_studio_parent_ids
        BEFORE INSERT ON runs WHEN EXISTS(SELECT 1 FROM studio_teams WHERE id=NEW.run_id)
        BEGIN SELECT RAISE(ABORT, 'Run ID is already a Studio parent'); END""")


def validate_task(task):
    if not isinstance(task, str) or not task.strip() or "\0" in task or len(task.encode("utf-8")) > 8192:
        raise ValueError("Task must be between 1 and 8192 UTF-8 bytes")


def normalize_subtasks(value, indices, developer_engine, reviewer_engine):
    if not isinstance(value, list) or not 2 <= len(value) <= 4:
        raise ValueError("병렬 하위 작업은 2~4개를 지정하세요.")
    if not isinstance(indices, list) or any(type(i) is not int for i in indices):
        raise ValueError("상위 작업의 폴더를 선택하세요.")
    normalized = []
    for item in value:
        if (not isinstance(item, dict) or not {"task", "indices"} <= item.keys()
                or item.keys() - {"task", "indices", "developer_engine", "reviewer_engine"}):
            raise ValueError("각 하위 작업의 목표와 작업 폴더를 지정하세요.")
        validate_task(item["task"])
        selection = item["indices"]
        if (not isinstance(selection, list) or not selection
                or any(type(index) is not int or index not in indices for index in selection)
                or len(set(selection)) != len(selection)):
            raise ValueError("하위 작업은 상위 작업에서 선택한 폴더를 사용하세요.")
        developer = item.get("developer_engine", developer_engine)
        reviewer = item.get("reviewer_engine", reviewer_engine)
        if developer not in SUPPORTED_ENGINES or reviewer not in SUPPORTED_ENGINES:
            raise ValueError("지원하는 개발자·리뷰어 엔진을 선택하세요.")
        normalized.append(dict(task=item["task"], indices=selection, developer_engine=developer,
                               reviewer_engine=reviewer))
    return normalized


def register(db, parent_id, digest, workspace_id, task, indices, folders, allow_edits,
             developer_engine, reviewer_engine, use_inbox, policy, revision):
    if db.execute("SELECT 1 FROM runs WHERE run_id=?", (parent_id,)).fetchone():
        raise OfficeError("REQUEST_CONFLICT")
    db.execute("""INSERT INTO studio_teams(id,request_hash,workspace_id,task,indices,folders,allow_edits,
        developer_engine,reviewer_engine,use_inbox,delegation,base_revision,host_name,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (parent_id, digest, workspace_id, snapshot_request(task), encode(indices), encode(folders),
         allow_edits, developer_engine, reviewer_engine, use_inbox, encode(policy) if policy else None,
         revision, platform.node(), now()))


def aggregate(children, stopping):
    if any(child["state"] in ACTIVE or child["state"] == "STOP_REQUESTED" for child in children):
        if stopping:
            return "STOP_REQUESTED", None
        if all(child["state"] == "QUEUED" for child in children):
            return "QUEUED", None
        return "RUNNING", None
    for state in ("RECOVERY_REQUIRED", "FAILED", "CANCELED", "WAITING_USER"):
        for child in children:
            if child["state"] == state:
                return state, child["outcome"]
    if stopping:
        return "CANCELED", "CANCELED"
    if all(child["state"] == "READY_FOR_HUMAN_REVIEW" for child in children):
        return "READY_FOR_HUMAN_REVIEW", "APPROVED"
    if all(child["state"] == "PLAN_ONLY" for child in children):
        return "PLAN_ONLY", "PLAN_ONLY"
    return "RECOVERY_REQUIRED", "INTERRUPTED"


def describe(db, row, *, detail):
    children = []
    for child in db.execute("""SELECT c.id,c.parent_id,c.indices,t.request_snapshot AS task,
            t.developer_engine,t.reviewer_engine,t.updated_at,r.outcome,
            CASE WHEN s.stop_requested AND r.ended_at IS NULL THEN 'STOP_REQUESTED' ELSE r.state END AS state
        FROM studio_team_children c JOIN studio_runs s ON s.id=c.id JOIN runs r ON r.run_id=c.id
        JOIN tasks t ON t.task_id=r.task_id WHERE c.parent_id=? ORDER BY c.position""", (row["id"],)):
        children.append(dict(child, indices=json.loads(child["indices"])))
    state, outcome = aggregate(children, row["stop_requested"])
    result = {field: row[field] for field in ("id", "workspace_id", "task", "developer_engine", "reviewer_engine",
                                               "created_at", "base_revision")}
    result.update(kind="parallel", state=state, outcome=outcome, allow_edits=bool(row["allow_edits"]),
                  use_inbox=bool(row["use_inbox"]), delegation=json.loads(row["delegation"]) if row["delegation"] else None,
                  delegation_active=False, updated_at=max(child["updated_at"] for child in children))
    if detail:
        result.update(folders=json.loads(row["folders"]), indices=json.loads(row["indices"]), subtasks=children)
    return result
