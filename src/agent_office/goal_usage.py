"""Typed, bounded provider receipts. Rates describe a pool, token counts describe a call."""
from datetime import datetime, timezone
import math
import platform
import re
from uuid import uuid4

from .store import now

COUNTERS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens")
CONTEXT = ("context_used", "context_window")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
BASE = dict(calls=[], total_tokens=None, unknown_calls=0, usage_coverage="unknown", rate_limits=[], decisions=[], hold=None)


def schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS studio_usage_calls(
        id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(run_id),role TEXT NOT NULL,stage TEXT NOT NULL,
        attempt INTEGER NOT NULL, engine TEXT NOT NULL, requested_model TEXT, requested_effort TEXT,
        account_alias TEXT,host TEXT NOT NULL,started_at TEXT NOT NULL,finished_at TEXT,
        source TEXT NOT NULL,observed_at TEXT,actual_model TEXT,actual_effort TEXT,
        input_tokens INTEGER,cached_input_tokens INTEGER,output_tokens INTEGER,reasoning_output_tokens INTEGER,
        total_tokens INTEGER,context_used INTEGER,context_window INTEGER)""")
    if "total_cost_usd" not in {row[1] for row in db.execute("PRAGMA table_info(studio_usage_calls)")}:
        db.execute("ALTER TABLE studio_usage_calls ADD COLUMN total_cost_usd REAL")
    db.execute("CREATE INDEX IF NOT EXISTS studio_usage_run ON studio_usage_calls(run_id)")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_usage_rates(
        id INTEGER PRIMARY KEY,run_id TEXT NOT NULL,role TEXT NOT NULL,engine TEXT NOT NULL,host TEXT NOT NULL,
        account_alias TEXT,account_key TEXT,origin TEXT NOT NULL,limit_id TEXT NOT NULL,name TEXT NOT NULL,
        used_percent REAL,window_minutes INTEGER,resets_at TEXT,ordinary_usage_allowed INTEGER,
        limit_reached INTEGER,spend_control_reached INTEGER,observed_at TEXT NOT NULL,source TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS studio_usage_rates_scope ON studio_usage_rates(engine,account_alias,account_key)")
    db.execute("CREATE INDEX IF NOT EXISTS studio_usage_rates_origin ON studio_usage_rates(origin,limit_id,name,id)")
    db.execute("CREATE INDEX IF NOT EXISTS studio_usage_rates_run ON studio_usage_rates(run_id,role)")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_usage_roles(
        run_id TEXT NOT NULL,role TEXT NOT NULL,attempt INTEGER NOT NULL,engine TEXT NOT NULL,
        PRIMARY KEY(run_id,role,attempt))""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_usage_actions(
        id INTEGER PRIMARY KEY,run_id TEXT NOT NULL,role TEXT NOT NULL,stage TEXT NOT NULL,
        attempt INTEGER NOT NULL,action TEXT NOT NULL,reason TEXT NOT NULL,at TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS studio_usage_actions_run ON studio_usage_actions(run_id,at)")
    db.execute("CREATE INDEX IF NOT EXISTS studio_usage_actions_run ON studio_usage_actions(run_id,id)")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_usage_holds(
        run_id TEXT PRIMARY KEY,role TEXT NOT NULL,stage TEXT NOT NULL,attempt INTEGER NOT NULL,
        kind TEXT NOT NULL,reason TEXT NOT NULL,at TEXT NOT NULL,engine TEXT,account_alias TEXT,next_probe_at TEXT)""")


def exists(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='studio_usage_calls'").fetchone())


def label(value):
    return value if isinstance(value, str) and IDENTIFIER.fullmatch(value) else None


def number(value):
    return value if type(value) is int and 0 <= value < 2**63 else None


def timestamp(value):
    try:
        if type(value) is int:
            return datetime.fromtimestamp(value, timezone.utc).isoformat()
        if isinstance(value, str) and len(value) <= 50:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is not None:
                return dt.astimezone(timezone.utc).isoformat()
    except (OverflowError, ValueError, OSError):
        pass
    return None


def begin(db, run_id, role, stage, attempt, selection):
    ident = uuid4().hex
    db.execute("""INSERT INTO studio_usage_calls(id,run_id,role,stage,attempt,engine,requested_model,
        requested_effort,account_alias,host,started_at,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,'dispatch')""",
        (ident,run_id,role,stage,attempt,selection["engine"],selection.get("model"),selection.get("effort"),
         selection.get("account_alias"),platform.node(),now()))
    return ident


def action(db, run_id, role, stage, attempt, kind, reason):
    db.execute("""INSERT INTO studio_usage_actions(run_id,role,stage,attempt,action,reason,at)
        VALUES(?,?,?,?,?,?,?)""", (run_id,role,stage,attempt,kind,reason,now()))


def record_rates(db, run_id, role, engine, alias, origin, event, source):
    key = event.get("account_key")
    key = key if isinstance(key,str) and re.fullmatch("[a-f0-9]{64}",key) else None
    limit = label(event.get("limit_id")) or "default"
    flags = [int(event[k]) if type(event.get(k)) is bool else None
             for k in ("ordinary_usage_allowed","limit_reached","spend_control_reached")]
    windows = event.get("windows", [])
    stamp = now()
    values = []
    for w in windows if isinstance(windows,list) else []:
        if not isinstance(w,dict) or w.get("name") not in ("primary","secondary"):
            continue
        used = w.get("used_percent")
        if type(used) not in (float,int) or not math.isfinite(used) or not 0 <= used <= 100:
            continue
        values.append((w["name"],used,number(w.get("window_minutes")),timestamp(w.get("resets_at"))))
    if any(f is not None for f in flags):
        values.append(("account",None,None,None))
    for name, used, minutes, reset in values:
        state = (key,used,minutes,reset,*flags)
        previous = db.execute("""SELECT account_key,used_percent,window_minutes,resets_at,ordinary_usage_allowed,
            limit_reached,spend_control_reached FROM studio_usage_rates WHERE origin=? AND limit_id=? AND name=?
            ORDER BY id DESC LIMIT 1""", (origin,limit,name)).fetchone()
        if previous is not None and tuple(previous) == state and source != "metadata_probe":
            continue
        db.execute("""INSERT INTO studio_usage_rates(run_id,role,engine,host,account_alias,account_key,origin,
            limit_id,name,used_percent,window_minutes,resets_at,ordinary_usage_allowed,limit_reached,
            spend_control_reached,observed_at,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (run_id,role,engine,platform.node(),alias,key,origin,limit,name,used,minutes,reset,*flags,stamp,source))


def observe(db, ident, event):
    call = db.execute("SELECT * FROM studio_usage_calls WHERE id=?", (ident,)).fetchone()
    if call is None or not isinstance(event,dict):
        return
    kind = event.get("kind")
    if kind == "rate_limit":
        record_rates(db,call["run_id"],call["role"],call["engine"],call["account_alias"],ident,event,"provider_event")
    elif kind == "usage":
        reported = number(event.get("total_tokens"))
        if reported is not None and call["total_tokens"] is not None and reported < call["total_tokens"]:
            return  # Older cumulative snapshots must not move context or freshness backwards.
        values = {}
        for k in COUNTERS + CONTEXT:
            value = number(event.get(k))
            if value is not None:
                values[k] = max(call[k] or 0,value) if k in COUNTERS else value
        cost = event.get("total_cost_usd")
        if type(cost) in (float,int) and 0 <= cost <= 1_000_000:
            values["total_cost_usd"] = cost
        if any(call[k] != v for k,v in values.items()):
            _update(db,ident,values,"provider_usage")
    elif kind == "model":
        values = {k:label(event.get(k)) for k in ("actual_model","actual_effort") if label(event.get(k))}
        if values:
            if (call["actual_model"] and values.get("actual_model") != call["actual_model"]
                    and "actual_effort" not in values):
                db.execute("UPDATE studio_usage_calls SET actual_effort=NULL WHERE id=?", (ident,))
            for field, value in values.items():
                old = call[field] or call[field.replace("actual","requested")]
                if old and old != value:
                    action(db,call["run_id"],call["role"],call["stage"],call["attempt"],"provider_change",
                           f"{field} {old} → {value} · 공급자 실행 응답")
            _update(db,ident,values,"provider_model")


def _update(db,ident,values,source):
    fields = ",".join(k+"=?" for k in values)
    db.execute(f"UPDATE studio_usage_calls SET {fields},source=?,observed_at=? WHERE id=?",
               (*values.values(),source,now(),ident))


def scope(row):
    if row["account_key"]:
        return "provider",row["account_key"]
    if row["account_alias"]:
        return "alias",row["account_alias"]
    return "unknown",row["origin"]


def same_pool(a,b):
    if a["engine"] != b["engine"] or a["limit_id"] != b["limit_id"]:
        return False
    if a["account_key"] and b["account_key"]:
        return a["account_key"] == b["account_key"]
    if a["account_alias"] and b["account_alias"]:
        return a["account_alias"] == b["account_alias"]
    return False


def rate_rows(db,run_id,role=None,selection=None):
    clause, args = "run_id=?", [run_id]
    if role is not None:
        clause, args = "run_id=? AND role=? AND engine=? AND account_alias IS ?", [
            run_id,role,selection["engine"],selection.get("account_alias")]
    # Select pools known to belong to this role or explicitly declared by the operator.
    alias = selection.get("account_alias") if selection else None
    shared = " OR (engine=? AND account_alias=?)" if alias else ""
    params = [*args,*( [selection["engine"],alias] if alias else [])]
    rows = db.execute(f"""WITH own AS (SELECT * FROM studio_usage_rates WHERE ({clause}){shared})
        SELECT r.* FROM studio_usage_rates r WHERE EXISTS(SELECT 1 FROM own o WHERE
        r.engine=o.engine AND (r.origin=o.origin OR r.account_key IS NOT NULL AND r.account_key=o.account_key
        OR r.account_alias IS NOT NULL AND r.account_alias=o.account_alias
        AND (r.account_key IS NULL OR o.account_key IS NULL OR r.account_key=o.account_key)))
        ORDER BY r.observed_at DESC,r.id DESC""",params)
    return [dict(row) for row in rows]


def exhausted(row):
    return (row["ordinary_usage_allowed"] == 0 or row["limit_reached"] == 1 or
            row["spend_control_reached"] == 1 or (row["used_percent"] or 0) >= 100)


def blockers(db,run_id,role,selection):
    rows = rate_rows(db,run_id,role,selection)
    model = selection.get("model")
    if not model:
        last = db.execute("""SELECT actual_model FROM studio_usage_calls WHERE run_id=? AND role=? AND engine=?
            AND requested_model IS NULL AND account_alias IS ? AND actual_model IS NOT NULL ORDER BY started_at DESC LIMIT 1""",
            (run_id,role,selection["engine"],selection.get("account_alias"))).fetchone()
        model = last[0] if last else None
    # A clock, a new CLI process or a falling percentage cannot open an exhausted pool.
    # A more recent explicit grant for that exact provider/account/bucket is required.
    grants = []
    for row in rows:
        if row["engine"] == "codex" and row["limit_id"] not in ("default", "codex", model):
            continue
        if exhausted(row):
            if not any(same_pool(row,grant) and grant["name"] == row["name"]
                       and (row["account_alias"] or row["account_key"])
                       and grant["observed_at"] > row["observed_at"] for grant in grants):
                return row
        elif row["ordinary_usage_allowed"] == 1:
            grants.append(row)
    return None


def brief(db,run_id):
    if not exists(db):
        return dict(hold=None,last_resume_at=None,last_progress_at=None)
    hold = db.execute("""SELECT kind,reason,stage,role,attempt,at FROM studio_usage_holds
        WHERE run_id=?""",(run_id,)).fetchone()
    resume,progress = db.execute("""SELECT MAX(CASE WHEN action='resumed' THEN at END),MAX(at)
        FROM studio_usage_actions WHERE run_id=?""",(run_id,)).fetchone()
    observed = db.execute("""SELECT MAX(coalesce(observed_at,started_at)) FROM studio_usage_calls
        WHERE run_id=?""",(run_id,)).fetchone()[0]
    public_hold = dict(hold) if hold else None
    if public_hold:
        public_hold["resume_condition"] = {
            "quota_wait":"같은 계정·한도 구간의 새 사용 가능 응답을 확인하면 같은 실행에서 재개합니다.",
            "quota_stop":"저장된 변경과 공급자 한도를 확인한 뒤 새 목표를 맡길 수 있습니다.",
            "budget_stop":"기록된 사용량과 후보를 검토한 뒤 새 예산으로 후속 목표를 맡길 수 있습니다.",
        }.get(public_hold["kind"])
    return dict(hold=public_hold,last_resume_at=resume,
                last_progress_at=max(filter(None,(observed,progress)),default=None))


def describe(db,run_id):
    if not exists(db):
        return {**BASE,**brief(db,run_id)}
    calls = [dict(row) for row in db.execute("SELECT * FROM studio_usage_calls WHERE run_id=? ORDER BY started_at,id",
                                            (run_id,))]
    for call in calls:
        call["context_basis"] = (("provider_context" if call["engine"] in ("cline", "gemini") else "last_response_total")
                                 if call["context_used"] is not None else None)
    seen = set()
    rates = []
    for row in rate_rows(db,run_id):
        key = row["engine"],scope(row),row["limit_id"],row["name"]
        if key in seen:
            continue
        seen.add(key)
        # Never serialize the raw provider account identifier. Unknown origins stay distinct.
        rates.append({k:v for k,v in row.items() if k not in {"id","run_id","origin"}} |
                     {"pool_scope":scope(row)[0],"pool_id":scope(row)[1]})
    decisions = [dict(row) for row in db.execute("""SELECT role,stage,attempt,action,reason,at
        FROM studio_usage_actions WHERE run_id=? ORDER BY id""",(run_id,))]
    known = [r["total_tokens"] for r in calls if r["total_tokens"] is not None]
    return dict(calls=calls, decisions=decisions, total_tokens=sum(known) if known else None,
                usage_coverage="unknown" if not known else "complete" if len(known)==len(calls) else "partial",
                unknown_calls=len(calls)-len(known),rate_limits=rates,**brief(db,run_id))
