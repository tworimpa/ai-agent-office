"""Validate terminal provider records before attributing a fixture result to a CLI."""
import json

from .adapters import AgentResult


def parse_fixture_completion(engine, output):
    invalid = AgentResult(1, "Fixture worker did not report one successful terminal result.")
    try:
        if engine == "claude":
            event = json.loads(output)
            if (not isinstance(event, dict) or event.get("type") != "result"
                    or event.get("subtype") != "success" or event.get("is_error") is not False
                    or event.get("error") or event.get("errors")
                    or ("stop_reason" in event and event["stop_reason"] not in ("end_turn", "stop_sequence"))):
                return invalid
            message = event.get("result")
        elif engine == "codex":
            records = [json.loads(line) for line in output.splitlines() if line.strip()]
            if not records or not all(isinstance(row, dict) for row in records):
                return invalid
            types = [row.get("type") for row in records]
            if (types.count("turn.started") != 1
                    or types[-1] != "turn.completed" or types.count("turn.completed") != 1
                    or any(kind in ("turn.failed", "error") for kind in types)):
                return invalid
            message = None
            for row in records[types.index("turn.started") + 1:]:
                item = row.get("item")
                if row.get("type") == "item.completed" and isinstance(item, dict):
                    if item.get("type") == "error" or item.get("status") == "failed":
                        return invalid
                    if item.get("type") == "agent_message":
                        message = item.get("text")
        else:
            return invalid
    except (TypeError, ValueError, RecursionError):
        return invalid
    if not isinstance(message, str) or not message.strip():
        return invalid
    return AgentResult(0, message[:12000], truncated=len(message) > 12000)
