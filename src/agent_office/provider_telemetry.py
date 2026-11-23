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


def acp_usage(data):
    # ACP's draft prompt receipt has session totals. A usage_update instead
    # describes context occupancy and must never be counted against a budget.
    if not isinstance(data, dict) or not bounded_int(data.get("totalTokens")):
        return None
    keys = {"totalTokens":"total_tokens", "inputTokens":"input_tokens", "outputTokens":"output_tokens",
            "thoughtTokens":"reasoning_output_tokens", "cachedReadTokens":"cached_input_tokens"}
    return {"kind":"usage", **{target:data[key] for key,target in keys.items() if bounded_int(data.get(key))}}


def batch_receipt(engine, output):
    import json
    from .acp import _unique_fields
    try:
        if engine == "claude":
            row = json.loads(output, object_pairs_hook=_unique_fields)
            if not isinstance(row, dict) or row.get("type") != "result":
                return []
        elif engine == "antigravity":
            rows = [json.loads(line, object_pairs_hook=_unique_fields) for line in output.split("\n") if line.strip()]
            if not rows or not all(isinstance(row, dict) for row in rows):
                return []
            if len(rows) == 1 and "event" not in rows[0] and "status" in rows[0]:
                row = rows[0]
            elif ([r.get("event") for r in rows].count("result") == 1 and rows[-1].get("event") == "result"
                  and all(r.get("event") in ("init","step_update") for r in rows[:-1])):
                row = rows[-1].get("result")
            else:
                return []
        else:
            return []
    except (ValueError, TypeError, RecursionError):
        return []
    if not isinstance(row, dict):
        return []
    usage = row.get("usage")
    event = {"kind":"usage"}
    if isinstance(usage, dict):
        if engine == "claude":
            keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            if (all(bounded_int(usage.get(k)) for k in keys[:2])
                    and all(bounded_int(usage[k]) for k in keys[2:] if k in usage)):
                total = sum(usage.get(k, 0) for k in keys)
                if bounded_int(total):
                    event.update(total_tokens=total, input_tokens=usage["input_tokens"],
                                 output_tokens=usage["output_tokens"])
                    if "cache_read_input_tokens" in usage:
                        event["cached_input_tokens"] = usage["cache_read_input_tokens"]
        else:
            for key,target in (("total_tokens","total_tokens"),("input_tokens","input_tokens"),
                               ("output_tokens","output_tokens"),("cache_read_tokens","cached_input_tokens"),
                               ("thinking_tokens","reasoning_output_tokens")):
                if bounded_int(usage.get(key)):
                    event[target] = usage[key]
    cost = row.get("total_cost_usd")
    if engine == "claude" and type(cost) in (float, int) and 0 <= cost <= 1_000_000:
        event["total_cost_usd"] = cost
    return [event] if len(event) > 1 else []


def claude_stream(output):
    import json
    from .acp import _unique_fields
    rows = [json.loads(line, object_pairs_hook=_unique_fields) for line in output.split("\n") if line.strip()]
    if (not rows or not all(isinstance(row, dict) for row in rows)
            or rows[-1].get("type") != "result" or any(row.get("type") == "result" for row in rows[:-1])):
        raise ValueError("Claude did not return one terminal stream result.")
    result = rows[-1]
    limits = []
    for row in rows[:-1]:
        if (row.get("type") != "rate_limit_event" or not result.get("session_id")
                or row.get("session_id") != result["session_id"]):
            continue
        info = row.get("rate_limit_info")
        if not isinstance(info, dict) or info.get("status") not in ("allowed", "allowed_warning", "rejected"):
            continue
        bucket = info.get("rateLimitType")
        if bucket not in ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet", "overage"):
            continue
        event = {"kind":"rate_limit", "limit_id":bucket, "windows":[],
                 "ordinary_usage_allowed":info["status"] != "rejected"}
        used = info.get("utilization")
        if type(used) in (int,float) and 0 <= used <= 1:
            window = {"name":"primary", "used_percent":used*100}
            if bounded_int(info.get("resetsAt"), 253402300799):
                window["resets_at"] = info["resetsAt"]
            event["windows"].append(window)
        limits.append(event)
    return json.dumps(result), limits
