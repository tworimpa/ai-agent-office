import json

import pytest

from agent_office.adapters import CLIAdapter, parse_output


def test_claude_command_read_only():
    command = CLIAdapter("claude").command("inspect", allow_edits=False)
    assert command[:2] == ["claude", "-p"]
    assert "inspect" not in command
    assert command[command.index("--permission-mode") + 1] == "plan"


def test_codex_command_read_only():
    command = CLIAdapter("codex").command("inspect", allow_edits=False)
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert command[-1] == "-"
    assert "inspect" not in command


def test_codex_command_edit():
    command = CLIAdapter("codex").command("implement", allow_edits=True)
    assert command[command.index("--sandbox") + 1] == "workspace-write"


def test_reject_unknown_engine():
    with pytest.raises(ValueError):
        CLIAdapter("unsupported")


def test_parse_claude_json():
    message, session = parse_output(
        "claude", json.dumps({"result": "done", "session_id": "abc"})
    )
    assert (message, session) == ("done", "abc")


def test_parse_codex_json_lines():
    output = "\n".join([
        json.dumps({"type": "thread.started", "thread_id": "xyz"}),
        json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "looks good"}}),
    ])
    assert parse_output("codex", output) == ("looks good", "xyz")
