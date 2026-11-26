"""Local bot conversations and operator-confirmed, independently dispatched orders."""
import hashlib
import json
import uuid

from . import local_bots
from .events import snapshot_request
from .models import OfficeError, validate_id
from .store import TaskStore, encode
from .team_runs import validate_task

CHAT_PAGE = 30
ORDER_PAGE = 20


def ensure_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS studio_bot_orders(
        workspace_id TEXT NOT NULL, id TEXT NOT NULL, digest TEXT NOT NULL,
        message TEXT NOT NULL, allow_edits INTEGER NOT NULL, indices TEXT,
        PRIMARY KEY(workspace_id,id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_bot_duties(
        workspace_id TEXT NOT NULL, order_id TEXT NOT NULL, position INTEGER NOT NULL,
        bot_id TEXT NOT NULL, duty TEXT NOT NULL, run_id TEXT NOT NULL UNIQUE, error TEXT,
        PRIMARY KEY(workspace_id,order_id,bot_id),
        FOREIGN KEY(workspace_id,order_id) REFERENCES studio_bot_orders(workspace_id,id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_bot_turns(
        run_id TEXT PRIMARY KEY REFERENCES studio_runs(id), workspace_id TEXT NOT NULL,
        bot_id TEXT NOT NULL, order_id TEXT, prior_context TEXT NOT NULL,
        FOREIGN KEY(workspace_id,order_id,bot_id) REFERENCES studio_bot_duties(workspace_id,order_id,bot_id))""")
    db.execute("CREATE INDEX IF NOT EXISTS studio_bot_conversation ON studio_bot_turns(workspace_id,bot_id)")
    db.execute("CREATE INDEX IF NOT EXISTS studio_bot_order_history ON studio_bot_orders(workspace_id)")


_FINAL_REPLY = """r.ended_at IS NOT NULL AND
    ((r.state='PLAN_ONLY' AND r.outcome='PLAN_ONLY') OR
     (r.state='READY_FOR_HUMAN_REVIEW' AND r.outcome='APPROVED'))"""
_TURNS = f"""SELECT b.run_id,t.request_snapshot,
    CASE WHEN {_FINAL_REPLY} THEN s.developer_output ELSE '' END AS developer_output,
    CASE WHEN s.stop_requested AND r.ended_at IS NULL THEN 'STOP_REQUESTED' ELSE r.state END AS state
    FROM studio_bot_turns b JOIN studio_runs s ON s.id=b.run_id AND s.workspace_id=b.workspace_id
    JOIN runs r ON r.run_id=s.id JOIN tasks t ON t.task_id=r.task_id
    WHERE b.workspace_id=? AND b.bot_id=?"""


def register_turn(db, workspace_id, bot_id, run_id, order_id):
    """Pin a bounded excerpt at admission, in the same transaction as the new run."""
    previous = db.execute(_TURNS + f" AND {_FINAL_REPLY} AND s.developer_output<>'' ORDER BY b.rowid DESC LIMIT 4",
                          (workspace_id, bot_id)).fetchall()
    history = []
    for row in reversed(previous):
        for role, column in (("user", "request_snapshot"), ("assistant", "developer_output")):
            if row[column]:
                raw = row[column].encode("utf-8")
                message = {"role": role, "text": raw[:1024].decode("utf-8", "ignore")}
                if len(raw) > 1024:
                    message["truncated"] = True
                history.append(message)
    db.execute("INSERT INTO studio_bot_turns VALUES(?,?,?,?,?)",
               (run_id, workspace_id, bot_id, order_id, encode(history)))


def recover_verified_turn(store, workspace_id, bot_id, run_id):
    # Legacy receipts pin the bot inside an opaque request digest. Only an exact
    # replay proves this was a direct command rather than a workspace role binding.
    db = store.connection
    if not db.execute("SELECT 1 FROM studio_bot_turns WHERE run_id=?", (run_id,)).fetchone():
        with store._write():
            db.execute("""INSERT INTO studio_bot_turns VALUES(?,?,?,NULL,'[]')
                ON CONFLICT(run_id) DO NOTHING""", (run_id, workspace_id, bot_id))


def prior_context(db, run_id):
    # The worker can also execute runs registered by a pre-conversation Studio.
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='studio_bot_turns'").fetchone():
        return None
    row = db.execute("SELECT prior_context FROM studio_bot_turns WHERE run_id=?", (run_id,)).fetchone()
    return json.loads(row[0]) if row else None


def _anchor(db, query, scope, cursor):
    row = db.execute(query, (*scope, validate_id(cursor))).fetchone()
    if row is None:
        raise OfficeError("NOT_FOUND")
    return row[0]


def chat(rooms, workspace_id, bot_id, before=None):
    rooms.catalog.get(workspace_id)
    with TaskStore(rooms.path, readonly=True) as store:
        db = store.connection
        db.execute("BEGIN")
        local_bots.get(db, workspace_id, bot_id)
        sql = _TURNS
        scope = (workspace_id, bot_id)
        if before is not None:
            anchor = _anchor(db, "SELECT rowid FROM studio_bot_turns WHERE workspace_id=? AND bot_id=? AND run_id=?",
                             scope, before)
            sql += " AND b.rowid<?"
            scope += (anchor,)
        rows = db.execute(sql + " ORDER BY b.rowid DESC LIMIT ?", (*scope, CHAT_PAGE + 1)).fetchall()
        cursor = rows[CHAT_PAGE - 1]["run_id"] if len(rows) > CHAT_PAGE else None
        messages = []
        for row in reversed(rows[:CHAT_PAGE]):
            for role, column in (("user", "request_snapshot"), ("assistant", "developer_output")):
                if row[column]:
                    messages.append({"id": row["run_id"] + "-" + role, "role": role, "text": row[column],
                                     "state": row["state"], "run_id": row["run_id"]})
    return {"messages": messages, "next_cursor": cursor}


def _digest(payload):
    return hashlib.sha256(encode(payload).encode("utf-8")).hexdigest()


def _orders(db, workspace_id, ids):
    if not ids:
        return []
    sql = """SELECT o.id,o.message,o.allow_edits,o.indices,o.digest,d.bot_id,d.duty,d.run_id,d.error,
        CASE WHEN s.stop_requested AND r.ended_at IS NULL THEN 'STOP_REQUESTED' ELSE r.state END AS state
        FROM studio_bot_orders o JOIN studio_bot_duties d ON d.workspace_id=o.workspace_id AND d.order_id=o.id
        LEFT JOIN studio_bot_turns b ON b.run_id=d.run_id AND b.workspace_id=d.workspace_id
            AND b.order_id=d.order_id AND b.bot_id=d.bot_id
        LEFT JOIN studio_runs s ON s.id=b.run_id LEFT JOIN runs r ON r.run_id=s.id
        WHERE o.workspace_id=?"""
    sql += " AND o.id IN (" + ",".join("?" for _ in ids) + ") ORDER BY o.rowid DESC,d.position"
    result = {}
    digests = {}
    for row in db.execute(sql, (workspace_id, *ids)):
        if row["id"] not in result:
            digests[row["id"]] = row["digest"]
            result[row["id"]] = {"id": row["id"], "message": row["message"], "assignments": [],
                                 "allow_edits": bool(row["allow_edits"])}
            if row["indices"] is not None:
                result[row["id"]]["indices"] = json.loads(row["indices"])
        assignment = {key: row[key] for key in ("bot_id", "duty", "run_id")}
        assignment["state"] = row["state"] or "pending"
        if row["state"] is None and row["error"]:
            assignment["error"] = row["error"]
        result[row["id"]]["assignments"].append(assignment)
    for order in result.values():
        # A reopened client can replay the public text verbatim only if the
        # journal's secret masking did not change any part of the submission.
        duties = [{"bot_id": item["bot_id"], "duty": item["duty"]} for item in order["assignments"]]
        order["replay_requires_original"] = _digest({**order, "assignments": duties}) != digests[order["id"]]
    return list(result.values())


def orders(rooms, workspace_id, before=None):
    rooms.catalog.get(workspace_id)
    with TaskStore(rooms.path, readonly=True) as store:
        db = store.connection
        db.execute("BEGIN")
        scope = (workspace_id,)
        sql = "SELECT id FROM studio_bot_orders WHERE workspace_id=?"
        if before is not None:
            anchor = _anchor(db, "SELECT rowid FROM studio_bot_orders WHERE workspace_id=? AND id=?", scope, before)
            sql += " AND rowid<?"
            scope += (anchor,)
        ids = [row[0] for row in db.execute(sql + " ORDER BY rowid DESC LIMIT ?", (*scope, ORDER_PAGE + 1))]
        return {"orders": _orders(db, workspace_id, ids[:ORDER_PAGE]),
                "next_cursor": ids[ORDER_PAGE - 1] if len(ids) > ORDER_PAGE else None}


def _task(message, duty):
    return f"왕명\n{message}\n\n이 봇은 아래에 배정된 업무만 수행하세요.\n{duty}"


def submit_order(rooms, workspace_id, data):
    workspace = rooms.catalog.get(workspace_id)
    required = {"id", "message", "assignments", "allow_edits"}
    if not isinstance(data, dict) or not required <= data.keys() or data.keys() - required - {"indices"}:
        raise ValueError("왕명과 확정한 봇별 담당 업무를 입력하세요.")
    order_id = validate_id(data["id"])
    message, assignments, edits = data["message"], data["assignments"], data["allow_edits"]
    validate_task(message)
    if type(edits) is not bool or not isinstance(assignments, list) or not assignments:
        raise ValueError("담당 봇과 파일 수정 허용 여부를 확인하세요.")
    duties = {}
    for item in assignments:
        if not isinstance(item, dict) or set(item) != {"bot_id", "duty"} or not isinstance(item["bot_id"], str):
            raise ValueError("각 봇의 ID와 담당 업무를 입력하세요.")
        bot_id, duty = item["bot_id"], item["duty"]
        validate_task(duty)
        validate_task(_task(message, duty))
        if bot_id in duties:
            raise ValueError("같은 봇에 배정할 일은 하나로 작성하세요.")
        duties[bot_id] = duty
    digest = _digest(data)
    with TaskStore(rooms.path, existing=True) as store:
        db = store.connection
        existing = db.execute("SELECT digest FROM studio_bot_orders WHERE workspace_id=? AND id=?",
                              (workspace_id, order_id)).fetchone()
        if existing is None:
            # Check all selected folders before reserving a new order. Receipt retries can
            # succeed after the original checkout disappears.
            rooms.catalog.select(workspace_id, data.get("indices", list(range(len(workspace["folders"])))))
        with store._write():
            inserted = db.execute("""INSERT INTO studio_bot_orders VALUES(?,?,?,?,?,?)
                ON CONFLICT(workspace_id,id) DO NOTHING""",
                (workspace_id, order_id, digest, snapshot_request(message), edits,
                 encode(data["indices"]) if "indices" in data else None)).rowcount
            if inserted:
                reviewer = local_bots.resolve(db, workspace_id)["reviewer"]["id"]
                for pos, (bot_id, duty) in enumerate(duties.items()):
                    if local_bots.get(db, workspace_id, bot_id)["builtin"]:
                        raise ValueError("회의실에서 직접 만든 봇을 선택하세요.")
                    if edits and reviewer == bot_id:
                        raise ValueError("독립 리뷰를 맡고 있는 봇에 파일 수정을 배정할 수 없습니다.")
                    child = uuid.uuid5(uuid.UUID(workspace_id), f"bot-order:{order_id}:{bot_id}").hex
                    db.execute("INSERT INTO studio_bot_duties VALUES(?,?,?,?,?,?,NULL)",
                               (workspace_id, order_id, pos, bot_id, snapshot_request(duty), child))
            elif db.execute("SELECT digest FROM studio_bot_orders WHERE workspace_id=? AND id=?",
                            (workspace_id, order_id)).fetchone()[0] != digest:
                raise OfficeError("REQUEST_CONFLICT")
        order = _orders(db, workspace_id, [order_id])[0]
        for item in order["assignments"]:
            if item["state"] != "pending":
                continue
            bot_id = item["bot_id"]
            body = {"id": item["run_id"], "message": _task(message, duties[bot_id]), "allow_edits": edits,
                    "meeting": False, "delegation": False}
            if "indices" in data:
                body["indices"] = data["indices"]
            try:
                rooms.assign(workspace_id, body, target_bot=bot_id, order_id=order_id)
            except (ValueError, OfficeError) as exc:
                if isinstance(exc, OfficeError) and exc.code not in {"NOT_FOUND", "RUN_BUSY", "REQUEST_CONFLICT"}:
                    raise
                # Keep other duties independent. Never replay an admitted run, even
                # if the supervisor failed to launch or its HTTP receipt was lost.
                with store._write():
                    db.execute("UPDATE studio_bot_duties SET error=? WHERE run_id=?",
                               (snapshot_request(str(exc)), item["run_id"]))
        return {"order": _orders(db, workspace_id, [order_id])[0]}
