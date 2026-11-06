"""Bounded, allowlisted persistence; raw worker output stays in memory."""
import json
import re
from .models import STATES, OUTCOMES

DOMAINS = {"phase": {"run", "workspace", "developer", "test", "review"},
           "role": {"developer", "tester", "reviewer"},
           "engine": {"claude", "codex", None}, "state": STATES, "outcome": OUTCOMES}


def snapshot_request(text: str) -> str:
    text = re.sub(r'''(?i)(\b[\w]*(?:API_KEY|TOKEN|SECRET|PASSWORD)\b\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)''', r"\1[redacted]", text)
    text = re.sub(r"(?i)(\bBearer\s+)[^\s,;]+", r"\1[redacted]", text)
    text = re.sub(r"\b(?:sk-ant-|sk-|ghp_|github_pat_)[A-Za-z0-9_-]+", "[redacted]", text)
    data = text.encode("utf-8")
    marker = b"...[truncated]"
    return text if len(data) <= 8192 else data[:8192-len(marker)].decode("utf-8", errors="ignore") + marker.decode()


def sanitize_payload(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Expected event object")
    result = {}
    for key, value in payload.items():
        if key in DOMAINS:
            if (value is not None and type(value) is not str) or value not in DOMAINS[key]:
                raise ValueError("Invalid metadata domain")
        elif key in ("attempt", "duration_ms", "returncode"):
            if type(value) is not int or (key != "returncode" and value < 0):
                raise ValueError("Invalid numeric metadata")
        elif key == "truncated":
            if type(value) is not bool:
                raise ValueError("Invalid truncation marker")
        else:
            continue
        result[key] = value
    return result


def encode_payload(payload: dict) -> str:
    data = json.dumps(sanitize_payload(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(data.encode("utf-8")) > 16384:
        raise ValueError("Event is too large")
    return data


class RunRecorder:
    def __init__(self, store, run_id: str):
        self.store = store
        self.run_id = run_id

    def record(self, **kwargs):
        return self.store.record_transition(self.run_id, **kwargs)
