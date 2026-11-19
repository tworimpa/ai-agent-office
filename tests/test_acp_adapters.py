"""Contract tests against a stdio subprocess, without invoking a vendor CLI."""

import json
import os
from pathlib import Path
import sys
import time

import pytest

from agent_office.acp import _permitted
from agent_office.adapters import CLIAdapter

PROMPT = "한글\n & |$(hello) inspect the workspace"
ANSWER = "VERDICT: APPROVED"


def run_fixture(tmp_path, monkeypatch, engine="cline", scenario="success", edit=False, strict=False):
    adapter = CLIAdapter(engine, timeout=0.5 if scenario == "timeout" else 10)
    command = adapter.command(PROMPT, allow_edits=edit)
    monkeypatch.setattr(adapter, "command", lambda *args, **kwargs: [
        sys.executable, __file__, engine, scenario, str(int(edit)), *command[1:],
    ])
    return adapter.execute(PROMPT, workspace=tmp_path, allow_edits=edit, strict_completion=strict)


@pytest.mark.parametrize("engine", ["gemini", "cline"])
@pytest.mark.parametrize("edit", [False, True])
def test_role_command_stdin_and_result(tmp_path, monkeypatch, engine, edit):
    result = run_fixture(tmp_path, monkeypatch, engine, edit=edit)
    assert (result.returncode, result.session_id, result.message) == (0, "fixture", ANSWER)
    assert not result.truncated


@pytest.mark.parametrize("engine", ["cline", "gemini"])
@pytest.mark.parametrize("scenario", ["partial", "wrong_session", "tool_error", "denied", "incomplete_tool",
                                      "bad_json", "duplicate", "rpc_error", "trailing", "exit_error", "no_marker", "truncated"])
def test_unverified_verdict_fails(tmp_path, monkeypatch, engine, scenario):
    result = run_fixture(tmp_path, monkeypatch, engine, scenario)
    if scenario == "truncated":
        assert result.truncated
    else:
        assert result.returncode and ANSWER not in result.message


@pytest.mark.parametrize("scenario", ["auto_approve", "numeric_false", "unknown_version", "nested_error",
                                      "outside", "wrong_kind", "review_write", "no_one_shot_permission"])
def test_cline_fails_closed(tmp_path, monkeypatch, scenario):
    assert run_fixture(tmp_path, monkeypatch, scenario=scenario).returncode


def test_gemini_file_evidence_and_mode_control(tmp_path, monkeypatch):
    assert run_fixture(tmp_path, monkeypatch, "gemini", "file_evidence", strict=True).returncode == 0
    for scenario in ("unlocated", "web_search", "wrong_mode_notice"):
        assert run_fixture(tmp_path, monkeypatch, "gemini", scenario, strict=True).returncode


def test_gemini_retry_replaces_partial_answer(tmp_path, monkeypatch):
    result = run_fixture(tmp_path, monkeypatch, "gemini", "retry")
    assert (result.returncode, result.message) == (0, "VERDICT: CHANGES_REQUESTED")
    for scenario in ("retry_no_reset", "retry_incomplete"):
        assert run_fixture(tmp_path, monkeypatch, "gemini", scenario).returncode


def test_timeout(tmp_path, monkeypatch):
    assert run_fixture(tmp_path, monkeypatch, scenario="timeout").returncode == 124


@pytest.mark.skipif(os.name != "nt", reason="Windows npm cmd launcher")
def test_timeout_stops_cmd_child(tmp_path, monkeypatch):
    launcher = tmp_path / "wrapper.cmd"
    child = tmp_path / "linger.py"
    child.write_text("from pathlib import Path\nimport time\nPath('started').touch()\n"
                     "time.sleep(3)\nPath('orphaned').touch()\n")
    launcher.write_text(f'@"{sys.executable}" "{child}"\n')
    monkeypatch.setattr("agent_office.adapters.resolve_executable", lambda name: str(launcher))
    start = time.monotonic()
    result = CLIAdapter("cline", timeout=1).execute(PROMPT, workspace=tmp_path, allow_edits=False)
    assert time.monotonic() - start < 8
    assert result.returncode == 124 and (tmp_path / "started").exists()
    time.sleep(max(0, start + 3.3 - time.monotonic()))
    assert not (tmp_path / "orphaned").exists()


def test_file_permissions(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "allowed.txt"
    target.write_text("read")
    for path in [tmp_path / "outside", workspace / ".git" / "HEAD", workspace / ".." / "outside",
                 workspace / "value.txt:stream", workspace / "dir." / "file", Path("relative")]:
        assert not _permitted(editor_call(path), workspace, True)
    assert _permitted(editor_call(target), workspace, True)
    assert not _permitted(editor_call(target), workspace, False)
    read = {"kind": "read", "title": "read_files: example", "rawInput": {"files": [{"path": str(target)}]}}
    assert _permitted(read, workspace, False)
    read["rawInput"]["files"].append({"path": str(tmp_path / "outside")})
    assert not _permitted(read, workspace, False)
    assert not _permitted({**editor_call(target), "kind": "execute"}, workspace, True)
    assert not _permitted({**editor_call(target), "title": "mcp_editor: pretend"}, workspace, True)
    link = workspace / "aliased"
    try:
        os.link(target, link)
    except OSError:
        return
    assert not _permitted(editor_call(link), workspace, True)


def editor_call(path):
    return {"toolCallId": "op", "kind": "edit", "title": "editor: file",
            "rawInput": {"path": str(path), "new_text": "edited"}}


def serve():
    engine, scenario, edit_flag, *args = sys.argv[1:]
    edit = edit_flag == "1"
    mode = ("act" if engine == "cline" else "autoEdit") if edit else "plan"
    assert args == (["--acp", "--auto-approve", "false"] if engine == "cline" else
                    ["--acp", "--approval-mode", "auto_edit" if edit else "plan"])
    if engine == "cline":
        assert os.environ["CLINE_SESSION_BACKEND_MODE"] == "local"

    def send(**record):
        print(json.dumps({"jsonrpc": "2.0", **record}, ensure_ascii=False), flush=True)

    def update(update_type, **data):
        send(method="session/update", params={"sessionId": "bad" if scenario == "wrong_session" else "fixture",
                                              "update": {"sessionUpdate": update_type, **data}})

    def receive(method):
        msg = json.loads(sys.stdin.readline())
        assert msg["method"] == method
        return msg

    if scenario == "timeout":
        time.sleep(20)
    init = receive("initialize")
    assert init["params"] == {"protocolVersion": 1, "clientCapabilities": {}}
    send(id=init["id"], result={"protocolVersion": 1, "agentInfo": {"name": engine,
         "version": "unreviewed" if scenario == "unknown_version" else "3.0.74"}})
    new = receive("session/new")
    assert new["params"] == {"cwd": str(Path.cwd()), "mcpServers": []}
    send(id=new["id"], result={"sessionId": "fixture", "modes": {"availableModes": [{"id": mode}]},
                               "configOptions": [{"id": "auto_approve", "currentValue":
                                     0 if scenario == "numeric_false" else scenario == "auto_approve"}]})
    change = receive("session/set_mode")
    assert change["params"] == {"sessionId": "fixture", "modeId": mode}
    if engine == "gemini":
        announced = "yolo" if scenario == "wrong_mode_notice" else mode
        update("agent_message_chunk", content={"type": "text", "text": "[MODE_UPDATE] " + announced})
    send(id=change["id"], result={})
    req = receive("session/prompt")
    assert req["params"]["prompt"][0]["text"].startswith(PROMPT + "\n")
    if scenario == "bad_json":
        print(ANSWER, flush=True)
        return
    if scenario == "duplicate":
        print('{"jsonrpc":"2.0","id":4,"result":{"stopReason":"cancelled","stopReason":"end_turn"}}', flush=True)
        return
    if scenario == "rpc_error":
        send(id=req["id"], error={"code": -32603, "message": ANSWER})
        return

    path = Path.cwd() / "example.txt"
    path.write_text("data")
    if engine == "cline" or scenario == "denied":
        if edit or scenario in ("review_write", "denied"):
            call = editor_call(path)
        else:
            call = {"toolCallId": "op", "kind": "read", "title": "read_files: example",
                    "rawInput": {"files": [{"path": str(path)}]}}
        if scenario == "outside":
            call["rawInput"]["files"][0]["path"] = str(path.parent.parent / "outside")
        if scenario == "wrong_kind":
            call["kind"] = "execute"
        opts = [{"kind": k, "optionId": k} for k in ("allow_once", "reject_once")]
        send(method="session/request_permission", id=100, params={"sessionId": "fixture",
             "toolCall": call, "options": [] if scenario == "no_one_shot_permission" else opts})
        decision = json.loads(sys.stdin.readline())
        if decision["result"]["outcome"].get("optionId") != "allow_once":
            return
        if scenario != "incomplete_tool":
            update("tool_call_update", toolCallId="op", status="failed" if scenario == "tool_error" else "completed",
                   rawOutput=[{"success": scenario != "nested_error"}])
    else:
        kind = "search" if scenario == "web_search" else ("edit" if edit else "read")
        update("tool_call", toolCallId="op", status="in_progress", kind=kind)
        if scenario != "incomplete_tool":
            update("tool_call_update", toolCallId="op", status="failed" if scenario == "tool_error" else "completed", kind=kind,
                   locations=[{"path": str(path)}] if scenario == "file_evidence" else [])
    if scenario.startswith("retry"):
        update("agent_message_chunk", content={"type": "text", "text": ANSWER + "\nAGENT_OFFICE_FINAL"})
        update("agent_thought_chunk", content={"type": "text", "text": "retry"}, _meta={"retry": True})
        if scenario != "retry_incomplete":
            meta = {"retry": True, "reset": True} if scenario == "retry" else {}
            update("agent_message_chunk", content={"type": "text", "text": "VERDICT: CHANGES_REQUESTED\nAGENT_OFFICE_FINAL"},
                   _meta=meta)
        send(id=req["id"], result={"stopReason": "end_turn"})
        return
    answer = ANSWER + ("x" * 12000 if scenario == "truncated" else "")
    if scenario != "no_marker":
        answer += "\nAGENT_OFFICE_FINAL"
    update("agent_message_chunk", content={"type": "text", "text": answer})
    send(id=req["id"], result={"stopReason": "max_tokens" if scenario == "partial" else "end_turn"})
    if scenario == "trailing":
        send(id=req["id"], result={"stopReason": "end_turn"})
    if scenario == "exit_error":
        sys.exit(8)


if __name__ == "__main__":
    serve()
