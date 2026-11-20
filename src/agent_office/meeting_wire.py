"""Validate meeting snapshots received from a named SSH peer."""
from datetime import datetime

from .engines import SUPPORTED_ENGINES
from .meeting import validate_plan


def check_decision_receipt(meeting, payload):
    """Bind the acknowledgement to this approval even if replay sees newer revisions."""
    revision = payload.get("revision")
    if (type(revision) is not int or not 1 <= revision <= meeting["revision"]
            or meeting.get("decision") != {"id": payload.get("id"), "revision": revision}):
        raise ValueError("Remote decision identity differs from the submitted approval")
    history = meeting["revisions"]
    accepted = history[revision - 1]
    if accepted["plan"] != payload.get("plan"):
        if revision >= len(history) or history[revision]["source"] != "operator":
            raise ValueError("Missing remote operator edit")
        accepted = history[revision]
    approved_at = accepted.get("approved_at")
    if (accepted["plan"] != payload.get("plan") or accepted.get("answers") != payload.get("answers")
            or accepted.get("approved_by") != "operator" or not isinstance(approved_at, str)
            or datetime.fromisoformat(approved_at).tzinfo is None):
        raise ValueError("Remote plan or answers differ from the submitted approval")


def check_meeting(value, run_id):
    if (not isinstance(value, dict) or value.get("run_id") != run_id
            or value.get("status") not in {"planning", "awaiting_review", "approved", "stopped", "failed"}
            or type(value.get("review_plan")) is not bool or type(value.get("revision")) is not int):
        raise ValueError("Invalid remote meeting identity")
    revisions = value.get("revisions")
    contributions = value.get("contributions")
    if (not isinstance(revisions, list) or len(revisions) != value["revision"] or len(revisions) > 64
            or not isinstance(contributions, list) or len(contributions) > 256):
        raise ValueError("Invalid remote meeting history")
    for number, entry in enumerate(revisions, 1):
        if (not isinstance(entry, dict) or entry.get("revision") != number
                or entry.get("source") not in {"coordinator", "operator", "direction"}):
            raise ValueError("Invalid remote plan revision")
        plan = validate_plan(entry.get("plan"))
        checks = entry.get("verification")
        if (not isinstance(checks, list) or len(checks) != len(plan["criteria"])
                or any(not isinstance(row, dict) or row.get("criterion") != criterion["criterion"]
                       or row.get("status") not in {"passed", "failed", "not_verified"}
                       or not isinstance(row.get("evidence"), str)
                       for row, criterion in zip(checks, plan["criteria"]))):
            raise ValueError("Invalid remote criterion evidence")
    latest = revisions[-1] if revisions else {}
    if (value.get("plan") != latest.get("plan") or value.get("verification") != latest.get("verification", [])
            or value.get("answers") != latest.get("answers", {})):
        raise ValueError("Remote plan differs from its revision")
    for opinion in contributions:
        if (not isinstance(opinion, dict) or opinion.get("role") not in {"coordinator", "developer", "reviewer"}
                or opinion.get("engine") not in SUPPORTED_ENGINES or not isinstance(opinion.get("text"), str)):
            raise ValueError("Invalid remote meeting contribution")
