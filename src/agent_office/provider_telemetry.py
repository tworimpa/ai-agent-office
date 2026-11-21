"""Allowlisted public metadata from provider protocol records."""
import hashlib
import re

from .events import redact_request


def safe_slug(value):
    return (isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,95}", value)
            and ".." not in value and redact_request(value) == value)


def bounded_int(value, maximum=2**53 - 1):
    return type(value) is int and 0 <= value <= maximum


def model_event(model, effort=None):
    if safe_slug(model):
        event = {"kind": "model", "actual_model": model}
        if safe_slug(effort):
            event["actual_effort"] = effort
        return event


_TOKEN_KEYS = {"inputTokens": "input_tokens", "cachedInputTokens": "cached_input_tokens",
               "outputTokens": "output_tokens", "reasoningOutputTokens": "reasoning_output_tokens",
               "totalTokens": "total_tokens"}


def usage_event(usage):
    if not isinstance(usage, dict) or not isinstance(usage.get("total"), dict):
        return None
    total = usage["total"]
    if not bounded_int(total.get("totalTokens")):
        return None
    event = {"kind": "usage", **{key: total[k] for k, key in _TOKEN_KEYS.items() if bounded_int(total.get(k))}}
    last = usage.get("last")
    if isinstance(last, dict) and bounded_int(last.get("totalTokens")):
        event["context_used"] = last["totalTokens"]
        event["context_basis"] = "last_response_total"
    if bounded_int(usage.get("modelContextWindow")) and usage["modelContextWindow"] > 0:
        event["context_window"] = usage["modelContextWindow"]
    return event


def exec_usage(usage):
    if not isinstance(usage, dict) or not all(bounded_int(usage.get(k)) for k in ("input_tokens", "output_tokens")):
        return None
    total = usage["input_tokens"] + usage["output_tokens"]
    if bounded_int(total):
        return {"kind": "usage", **{k: usage[k] for k in _TOKEN_KEYS.values() if bounded_int(usage.get(k))},
                "total_tokens": total}


def limit_event(snapshot, *, account_key=None, ordinary=None, bucket=None):
    if not isinstance(snapshot, dict):
        return None
    limit = snapshot.get("limitId") or bucket
    if limit is not None and not safe_slug(limit):
        return None
    windows = []
    for key in ("primary", "secondary"):
        window = snapshot.get(key)
        if not isinstance(window, dict) or not bounded_int(window.get("usedPercent"), 100):
            continue
        record = {"name": key, "used_percent": window["usedPercent"]}
        if bounded_int(window.get("resetsAt"), 253402300799):
            record["resets_at"] = window["resetsAt"]
        if bounded_int(window.get("windowDurationMins")):
            record["window_minutes"] = window["windowDurationMins"]
        windows.append(record)
    event = {"kind": "rate_limit", "limit_id": limit, "windows": windows}
    if account_key:
        event["account_key"] = account_key
    if type(ordinary) is bool:
        event["ordinary_usage_allowed"] = ordinary
    if type(snapshot.get("spendControlReached")) is bool:
        event["spend_control_reached"] = snapshot["spendControlReached"]
    if snapshot.get("rateLimitReachedType") in ("rate_limit_reached", "workspace_owner_credits_depleted",
            "workspace_member_credits_depleted", "workspace_owner_usage_limit_reached", "workspace_member_usage_limit_reached"):
        event["limit_reached"] = True
    return event if windows or any(k in event for k in ("ordinary_usage_allowed", "spend_control_reached", "limit_reached")) else None


def quota_response(result):
    identity = result.get("accountId")
    account = (hashlib.sha256(("codex-account\0" + identity).encode("utf-8")).hexdigest()
               if isinstance(identity, str) and 0 < len(identity) <= 256 else None)
    buckets = result.get("rateLimitsByLimitId")
    if buckets is not None and (not isinstance(buckets, dict) or len(buckets) > 64):
        raise ValueError("Invalid quota metadata.")
    rows = {}
    for bucket, value in [(None, result.get("rateLimits")), *(buckets or {}).items()]:
        if bucket is not None and (not safe_slug(bucket) or not isinstance(value, dict)
                                   or value.get("limitId") not in (None, bucket)):
            continue
        identifier = (value.get("limitId") or bucket) if isinstance(value, dict) else bucket
        ordinary = result.get("ordinaryUsageAllowed") if identifier in (None, "codex") else None
        event = limit_event(value, account_key=account, bucket=bucket, ordinary=ordinary)
        if event:
            rows[event["limit_id"]] = event
    if not any(k in rows for k in (None, "codex")) and type(result.get("ordinaryUsageAllowed")) is bool:
        rows["codex"] = limit_event({}, account_key=account, bucket="codex", ordinary=result["ordinaryUsageAllowed"])
    return account, list(rows.values())


def applicable_limit(event, model):
    return event is not None and event["limit_id"] in (None, "codex", model)


def depleted(event):
    return (event.get("ordinary_usage_allowed") is False or event.get("limit_reached") is True
            or event.get("spend_control_reached") is True
            or any(window["used_percent"] >= 100 for window in event["windows"]))
