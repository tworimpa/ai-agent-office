import pytest

from agent_office.adapters import CLIAdapter, record_exec_usage
from agent_office.provider_profiles import validate_selection
from agent_office.provider_telemetry import limit_event, quota_response, usage_event


@pytest.mark.parametrize(("engine", "effort", "expected"), [
    ("codex", "high", ["--model", "fixture", "-c", "model_reasoning_effort=high", "-"]),
    ("antigravity", "xhigh", ["--model", "fixture", "--effort", "xhigh"]),
    ("claude", "medium", ["--model", "fixture", "--effort", "medium"]),
    ("gemini", None, ["--model", "fixture"]),
])
def test_per_call_selection_keeps_default_sandbox_and_stdin(engine, effort, expected):
    adapter = CLIAdapter(engine)
    default = adapter.command("private prompt", allow_edits=False)
    chosen = adapter.command("private prompt", allow_edits=False, model="fixture", effort=effort)
    assert chosen[-len(expected):] == expected
    assert chosen[:len(default) - (engine == "codex")] == (default[:-1] if engine == "codex" else default)
    assert adapter.command("another prompt", allow_edits=False) == default


@pytest.mark.parametrize(("engine", "model", "effort"), [
    ("cline", "fixture", None), ("gemini", None, "high"), ("claude", None, "ultra"),
    ("codex", "model & echo marker", None), ("codex", "--model", None), ("codex", "sk-fake-secret", None),
    ("antigravity", "model%PATH%", None),
])
def test_invalid_selection_fails_before_process_launch(engine, model, effort):
    with pytest.raises(ValueError):
        validate_selection(engine, model, effort)


def test_exec_usage_excludes_cached_and_reasoning_subtotals_from_sum():
    events = []
    record_exec_usage('{"type":"turn.completed","usage":{"input_tokens":50,"cached_input_tokens":44,'
                      '"output_tokens":10,"reasoning_output_tokens":5,"private":"private"}}', events.append)
    assert events == [{"kind": "usage", "input_tokens": 50, "cached_input_tokens": 44,
                       "output_tokens": 10, "reasoning_output_tokens": 5, "total_tokens": 60}]
    assert not usage_event({"total": {"totalTokens": True}})
    assert not limit_event({"limitId": "sk-fake-secret", "primary": {"usedPercent": 1}})


def test_quota_preserves_identity_independently_of_ordinary_usage():
    bucket = {"limitId": "codex", "secondary": {"usedPercent": 100, "windowDurationMins": 10080, "resetsAt": 203}}
    _, entries = quota_response({"ordinaryUsageAllowed": True, "rateLimits": bucket})
    assert entries == [{"kind": "rate_limit", "limit_id": "codex", "ordinary_usage_allowed": True, "windows": [
        {"name": "secondary", "used_percent": 100, "window_minutes": 10080, "resets_at": 203}]}]
    key, entries = quota_response({"accountId": "private", "ordinaryUsageAllowed": False, "rateLimits": bucket,
                                  "rateLimitsByLimitId": {"codex": {**bucket, "spendControlReached": True}}})
    assert len(key) == 64 and len(entries) == 1
    assert entries[0]["ordinary_usage_allowed"] is False and entries[0]["spend_control_reached"] is True
