"""Allowlisted Studio improvements routes and journal-bound SSH response contracts."""
from datetime import datetime
import re

from .engines import SUPPORTED_ENGINES
from .models import TERMINAL_OUTCOMES, validate_id


def improvement_route(method, route):
    if method == "GET":
        if route == "/improvements":
            return "status", None
        page = re.fullmatch(r"/improvements/(runs|jobs|candidates)/page/(0|[1-9][0-9]{0,15})", route)
        if page:
            offset = int(page[2])
            if offset < 2**53 and offset % 24 == 0:
                return page[1], offset
        item = re.fullmatch(r"/improvements/(jobs|candidates)/([a-f0-9]{32})", route)
        if item:
            return ("job" if item[1] == "jobs" else "candidate"), item[2]
    if method == "POST" and route == "/improvements/jobs":
        return "start", None
    return None


def _require(condition):
    if not condition:
        raise ValueError("Invalid improvements response")


def _keys(value, fields):
    _require(isinstance(value, dict) and set(value) == set(fields.split()))


def _sha(value):
    _require(isinstance(value, str) and re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", value))


def _text(value, limit):
    _require(isinstance(value, str) and len(value.encode("utf-8")) <= limit)


def _timestamp(value):
    _text(value, 48)
    _require(datetime.fromisoformat(value).tzinfo is not None)


def _run_ids(ids):
    _require(isinstance(ids, list) and 1 <= len(ids) <= 8)
    for rid in ids:
        validate_id(rid)
    _require(len(set(ids)) == len(ids))


def _studio_links(links, selected):
    _require(isinstance(links, list) and len(links) <= len(selected))
    _require(all(isinstance(link, str) and link in selected for link in links) and len(links) == len(set(links)))


def check_start(payload):
    _keys(payload, "id base engine run_ids")
    validate_id(payload["id"])
    _sha(payload["base"])
    _require(payload["engine"] in SUPPORTED_ENGINES)
    _run_ids(payload["run_ids"])


def _job(row):
    _keys(row, "id base engine run_ids state title message")
    check_start({k: row[k] for k in ("id", "base", "engine", "run_ids")})
    _require(row["state"] in ("generating", "ready", "no_proposal", "failed", "interrupted"))
    _text(row["title"], 1024)
    _text(row["message"], 8192)


def _candidate(row, detail=False):
    fields = "id job_id sha base state title validation decision"
    extra = " diff expected rationale image run_ids history validation_output exit_code diff_truncated output_truncated history_omitted"
    _keys(row, fields + (extra if detail else ""))
    validate_id(row["id"])
    if row["job_id"] is not None:
        validate_id(row["job_id"])
    _sha(row["sha"])
    _sha(row["base"])
    _text(row["title"], 1024)
    state, result, decision = row["state"], row["validation"], row["decision"]
    allowed = {"submitted": (None, "running"), "failed": ("failed", "running", None),
               "passed": ("passed",), "approved": ("passed",), "rejected": (None, "passed", "failed")}
    _require(isinstance(state, str) and result in allowed.get(state, ()))
    _require(decision == (state if state in ("approved", "rejected") else None))
    if not detail:
        return
    _require(isinstance(row["image"], str) and re.fullmatch(r"sha256:[a-f0-9]{64}", row["image"]))
    _run_ids(row["run_ids"])
    _text(row["expected"], 2400)
    _text(row["rationale"], 2400)
    _text(row["diff"], 50 * 1024)
    if row["validation_output"] is not None:
        _text(row["validation_output"], 17 * 1024)
    _require(row["exit_code"] is None or type(row["exit_code"]) is int)
    if result == "passed":
        _require(type(row["exit_code"]) is int and row["exit_code"] == 0)
    _require(all(type(row[k]) is bool for k in ("diff_truncated", "output_truncated")))
    _require(type(row["history_omitted"]) is int and 0 <= row["history_omitted"] < 2**53)
    _require(isinstance(row["history"], list) and 1 <= len(row["history"]) <= 32)
    latest_check = None
    for event in row["history"]:
        _keys(event, "event time")
        _require(event["event"] in ("submitted", "validation_started", "validation_finished", "approved", "rejected"))
        _timestamp(event["time"])
        if event["event"].startswith("validation_"):
            latest_check = event["event"]
    _require(not any(e["event"] in ("approved", "rejected") for e in row["history"][:-1]))
    if result in ("passed", "failed"):
        _require(latest_check == "validation_finished" and row["validation_output"] is not None)
    else:
        _require(row["exit_code"] is None and latest_check != "validation_finished")
    if result == "running":
        _require(latest_check == "validation_started")
    if decision:
        _require(row["history"] and row["history"][-1]["event"] == decision)
    else:
        _require(row["history"][-1]["event"] not in ("approved", "rejected"))


def check_improvement_reply(status, data, target, payload):
    kind, arg = target
    _require(status == (202 if kind == "start" else 200))
    if kind == "status":
        _keys(data, "source_enabled view")
        view = data["view"]
        _keys(view, "available stable updated_at")
        _require(type(data["source_enabled"]) is bool and type(view["available"]) is bool)
        if view["available"]:
            _sha(view["stable"])
            _timestamp(view["updated_at"])
        else:
            _require(view["stable"] is None and view["updated_at"] is None)
    elif kind in ("runs", "jobs", "candidates"):
        _keys(data, "items next_offset")
        rows = data["items"]
        _require(isinstance(rows, list) and len(rows) <= 24)
        cursor = data["next_offset"]
        _require(cursor is None or (type(cursor) is int and cursor == arg + 24 and len(rows) == 24))
        for row in rows:
            if kind == "jobs":
                _job(row)
            elif kind == "candidates":
                _candidate(row)
            else:
                _keys(row, "id request state outcome")
                validate_id(row["id"])
                _text(row["request"], 8192)
                _require(isinstance(row["state"], str) and isinstance(row["outcome"], str)
                         and row["outcome"] in TERMINAL_OUTCOMES.get(row["state"], ()))
        _require(len({r["id"] for r in rows}) == len(rows))
    elif kind == "candidate":
        _keys(data, "candidate studio_run_ids")
        _candidate(data["candidate"], detail=True)
        _require(data["candidate"]["id"] == arg)
        _studio_links(data["studio_run_ids"], data["candidate"]["run_ids"])
    elif kind == "start":
        _keys(data, "job")
        _job(data["job"])
        check_start(payload)
        _require(all(data["job"][k] == v for k, v in payload.items()))
    else:
        _keys(data, "job candidate_id error proposal studio_run_ids publication_available")
        _require(type(data["publication_available"]) is bool)
        if not data["publication_available"]:
            _require(data["candidate_id"] is None and data["error"] is None)
        _job(data["job"])
        _require(data["job"]["id"] == arg)
        _studio_links(data["studio_run_ids"], data["job"]["run_ids"])
        if data["candidate_id"] is not None:
            validate_id(data["candidate_id"])
            _require(data["job"]["state"] == "ready" and data["error"] is None)
        if data["error"] is not None:
            _text(data["error"], 8192)
        proposal = data["proposal"]
        _require((proposal is not None) == (data["job"]["state"] == "ready"))
        if proposal is not None:
            _keys(proposal, "rationale expected patch")
            _text(proposal["expected"], 2400)
            _text(proposal["rationale"], 2400)
            _text(proposal["patch"], 32768)
