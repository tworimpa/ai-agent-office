"""A host-local read of actual process health; no service control over HTTP."""
import re
from datetime import datetime

from .models import OfficeError, validate_id
from .version_wire import MAX_SEQUENCE


_SHA = r"(?:[0-9a-f]{40}|[0-9a-f]{64})"
_SELECTED = re.compile(rf"/deployment/versions/([1-9][0-9]{{0,15}})/({_SHA})")
STATES = ("deploying", "restoring", "healthy", "deploy_failed_restored", "deploy_failed_stopped", "restore_failed")


def deployment_route(method, route):
    if method != "GET":
        return None
    if route == "/deployment":
        return ()
    match = _SELECTED.fullmatch(route)
    if match and int(match[1]) <= MAX_SEQUENCE:
        return int(match[1]), match[2]
    return None


def _matches(view, journal, release, selection):
    from .release_view import _reader
    from .stable_view import _head

    if selection is None or view is None:
        return None
    try:
        with _reader(view, journal) as (db, meta):
            if meta["release_id"] != release:
                return False
            head, _ = _head(db, meta)
            if head is None:
                return None
            row = db.execute("SELECT sha FROM stable_events WHERE seq=?", (selection["seq"],)).fetchone()
            return row is not None and row["sha"] == selection["sha"]
    except OfficeError:
        # A failed publication read still permits an independent process observation.
        return None


def read_status(target, view, journal, reference):
    selection = {"seq": reference[0], "sha": reference[1]} if reference else None
    body = dict(configured=target is not None, selection=selection, service=None, runtime_journal_id=None, release_matches=None,
                running=None, health="unconfigured", last=None)
    if target is None:
        return body
    from .deployment import public_status

    status = public_status(target)
    body["service"] = status["service"]
    body["runtime_journal_id"] = status["journal_id"]
    body["health"] = status["health"]
    body["release_matches"] = _matches(view, journal, status["release_id"], selection)
    if status["health"] == "healthy":
        body["running"] = {k: status["actual"][k] for k in ("seq", "sha", "boot_id")}
    last = status["last"]
    if last:
        body["last"] = {k: last[k] for k in ("id", "state", "time", "message")}
        for side in ("requested", "previous"):
            body["last"][side] = {k: last[side][k] for k in ("seq", "sha")} if last[side] else None
    return body


def _require(condition):
    if not condition:
        raise ValueError("Invalid deployment status response")


def _keys(value, keys):
    _require(isinstance(value, dict) and value.keys() == set(keys.split()))


def _revision(row):
    _require(type(row["seq"]) is int and 1 <= row["seq"] <= MAX_SEQUENCE)
    _require(isinstance(row["sha"], str) and re.fullmatch(_SHA, row["sha"]))


def check_reply(code, body, reference):
    _require(code == 200)
    _keys(body, "configured service runtime_journal_id selection release_matches running health last")
    _require(type(body["configured"]) is bool)
    choice, match, observation = body["selection"], body["release_matches"], body["running"]
    _require(match is None or type(match) is bool)
    if reference:
        _keys(choice, "seq sha")
        _revision(choice)
        _require((choice["seq"], choice["sha"]) == reference)
    else:
        _require(choice is None and match is None)
    if not body["configured"]:
        _require(body["health"] == "unconfigured" and observation is None and match is None
                 and body["last"] is None and body["service"] is None and body["runtime_journal_id"] is None)
        return
    _require(isinstance(body["service"], str) and 0 < len(body["service"]) <= 256)
    validate_id(body["runtime_journal_id"])
    _require(body["health"] in ("healthy", "offline", "unverified"))
    if body["health"] == "healthy":
        _keys(observation, "seq sha boot_id")
        _revision(observation)
        validate_id(observation["boot_id"])
    else:
        _require(observation is None)
    last = body["last"]
    if last is not None:
        _keys(last, "id state requested previous time message")
        validate_id(last["id"])
        _require(last["state"] in STATES)
        _require(isinstance(last["message"], str) and len(last["message"].encode("utf-8")) <= 4096)
        _require(isinstance(last["time"], str) and len(last["time"]) <= 48
                 and datetime.fromisoformat(last["time"]).tzinfo is not None)
        for side in ("requested", "previous"):
            revision = last[side]
            if side == "previous" and revision is None:
                continue
            _keys(revision, "seq sha")
            _revision(revision)
        if last["state"] == "deploy_failed_restored":
            _require(last["previous"] is not None)
