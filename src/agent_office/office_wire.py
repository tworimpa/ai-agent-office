"""Read-only office route and bounded peer snapshot contract."""
from datetime import datetime
import re

from .dialogue import PRODUCT_DECISION, REQUEST_KINDS
from .engines import SUPPORTED_ENGINES
from .models import ACTIVE, STATES, validate_id
from .office_state import ACTIVITIES, PAGE_SIZE, PREVIEW_LIMIT, RECENT_RUNS, REQUEST_PREVIEWS, TASK_LIMIT
from .role_skills import ROLES
from .studio_inbox import ROLE_STATE


def office_offset(route):
    if route == "/office":
        return 0
    match = re.fullmatch(r"/office/page/([1-9][0-9]{0,15})", route)
    if match:
        offset = int(match[1])
        if offset < 2**53 and offset % PAGE_SIZE == 0:
            return offset
    return None


def _count(value):
    if type(value) is not int or not 0 <= value < 2**53:
        raise ValueError("Invalid office count")
    return value


def _text(value, limit):
    if not isinstance(value, str) or not 0 < len(value) <= limit:
        raise ValueError("Invalid office preview")


def _timestamp(value):
    if not isinstance(value, str) or len(value) > 40 or datetime.fromisoformat(value).tzinfo is None:
        raise ValueError("Missing journal timestamp")


def check_office_reply(data, offset):
    """Validate links, role/request attribution and page boundaries before forwarding."""
    _timestamp(data.get("generated_at"))
    total = _count(data.get("total_runs"))
    if _count(data.get("offset")) != offset or "next_offset" not in data:
        raise ValueError("Wrong office page")
    expected_next = offset + PAGE_SIZE if offset + PAGE_SIZE < total else None
    cursor = data["next_offset"]
    if cursor is not None:
        _count(cursor)
    if cursor != expected_next:
        raise ValueError("Wrong office page cursor")
    counts = data.get("counts")
    if not isinstance(counts, dict) or not 0 <= total - _count(counts.get("active_runs")) <= RECENT_RUNS:
        raise ValueError("Invalid office summary")
    request_counts = {"question": _count(counts.get("questions")),
                      "permission": _count(counts.get("permissions")),
                      PRODUCT_DECISION: _count(counts.get("product_decisions", 0))}
    rows = data.get("agents")
    if not isinstance(rows, list) or len(rows) > PAGE_SIZE * len(ROLES):
        raise ValueError("Invalid agent list")
    seats, runs, ids = set(), {}, set()
    visible = dict.fromkeys(REQUEST_KINDS, 0)
    page_request_count = 0
    for agent in rows:
        if not isinstance(agent, dict):
            raise ValueError("Invalid agent")
        rid = validate_id(agent.get("run_id"))
        parent = agent.get("parent_id")
        if parent is not None:
            if validate_id(parent) == rid:
                raise ValueError("Invalid parent link")
            _text(agent.get("parent_task"), TASK_LIMIT)
        elif agent.get("parent_task") is not None:
            raise ValueError("Unbound parent task")
        role, activity, state = agent.get("role"), agent.get("activity"), agent.get("run_state")
        if (role not in ROLES or agent.get("engine") not in SUPPORTED_ENGINES
                or not isinstance(activity, str) or activity not in ACTIVITIES
                or not isinstance(state, str) or state not in STATES | {"STOP_REQUESTED"}):
            raise ValueError("Invalid assignment")
        _text(agent.get("task"), TASK_LIMIT)
        _timestamp(agent.get("updated_at"))
        identity = (parent, agent.get("parent_task"), agent["task"], state)
        if runs.setdefault(rid, identity) != identity or (rid, role) in seats:
            raise ValueError("Conflicting agent assignment")
        seats.add((rid, role))
        previews = agent.get("requests")
        count = _count(agent.get("request_count"))
        if (not isinstance(previews, list) or len(previews) != min(count, REQUEST_PREVIEWS)
                or (count > 0) != (activity == "waiting")
                or count and ROLE_STATE.get(role) != state):
            raise ValueError("Invalid pending requests")
        page_request_count += count
        for req in previews:
            if not isinstance(req, dict) or req.get("kind") not in REQUEST_KINDS:
                raise ValueError("Invalid request kind")
            request_id = validate_id(req.get("id"))
            if request_id in ids:
                raise ValueError("Duplicate request")
            ids.add(request_id)
            _text(req.get("prompt"), PREVIEW_LIMIT)
            visible[req["kind"]] += 1
    active_on_page = sum(info[3] in ACTIVE or info[3] == "STOP_REQUESTED" for info in runs.values())
    all_requests = sum(request_counts.values())
    if (len(runs) != min(PAGE_SIZE, max(total - offset, 0)) or any((rid, "developer") not in seats for rid in runs)
            or active_on_page != min(PAGE_SIZE, max(0, counts["active_runs"] - offset))
            or page_request_count > all_requests
            or (not counts["active_runs"] and all_requests)
            or (offset == 0 and (total <= PAGE_SIZE or counts["active_runs"] <= PAGE_SIZE) and page_request_count != all_requests)
            or any(visible[kind] > request_counts[kind] for kind in visible)):
        raise ValueError("Office totals contradict visible assignments")
