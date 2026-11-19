"""Contract tests against a stdio subprocess, without invoking a vendor CLI."""

import asyncio
import json
import os
from contextlib import contextmanager
from pathlib import Path
import sys
import time

import pytest

from agent_office.acp import _permitted, write_record
from agent_office.adapters import CLIAdapter

PROMPT = "한글\n & |$(hello) inspect the workspace"
ANSWER = "VERDICT: APPROVED"


def run_fixture(tmp_path, monkeypatch, engine="cline", scenario="success", edit=False, strict=False, inbox=None):
    adapter = CLIAdapter(engine, timeout=0.5 if scenario == "timeout" else 10)
    command = adapter.command(PROMPT, allow_edits=edit)
    monkeypatch.setattr(adapter, "command", lambda *args, **kwargs: [
        sys.executable, __file__, engine, scenario, str(int(edit)), *command[1:],
    ])
    return adapter.execute(PROMPT, workspace=tmp_path, allow_edits=edit, strict_completion=strict, inbox=inbox)


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


class Answers:
    def __init__(self, permission="approve"):
        self.permission = permission
        self.requests = []
        self.sent = []
        self.receipts = []

    async def ask(self, kind, prompt, session):
        assert session == "fixture"
        self.requests.append((kind, prompt))
        return {"id": len(self.requests), "decision": self.permission if kind == "permission" else "answer",
                "answer": None if kind == "permission" else "사용자 답변\n& |"}

    @contextmanager
    def sending(self, request):
        assert request not in self.sent
        self.sent.append(request)
        yield

    def delivered(self, request):
        assert request in self.sent and request not in self.receipts
        self.receipts.append(request)


def test_inbox_guard_expiring_before_write_delivers_no_bytes():
    class BusyInbox:
        @contextmanager
        def sending(self, request):
            assert request == 42
            time.sleep(0.06)
            yield

    async def send():
        deadline = asyncio.get_running_loop().time() + 0.03
        # No process is needed: touching stdin would itself fail the test.
        await write_record(None, b"answer\n", inbox=BusyInbox(), decision_id=42, deadline=deadline)

    with pytest.raises(TimeoutError):
        asyncio.run(send())


@pytest.mark.parametrize("engine,decision", [("cline", "approve"), ("cline", "reject"), ("gemini", None)])
def test_same_session_question_and_native_permission(tmp_path, monkeypatch, engine, decision):
    inbox = Answers(decision)
    result = run_fixture(tmp_path, monkeypatch, engine, "inbox_flow", edit=True, inbox=inbox)
    assert (result.returncode, result.session_id, result.message) == (0, "fixture", "finished")
    assert inbox.requests[0] == ("question", "어느 값인가요?")
    if engine == "cline":
        kind, description = inbox.requests[1]
        name, data = description.split("\n", 1)
        assert (kind, name, json.loads(data)) == ("permission", "editor", editor_call(tmp_path / "target")["rawInput"])
        assert (tmp_path / "target").exists() == (decision == "approve")
    else:
        assert len(inbox.requests) == 1
    assert sorted(inbox.receipts) == inbox.sent == list(range(1, len(inbox.requests) + 1))


@pytest.mark.parametrize("engine,scenario", [
    ("cline", "wrong_kind"), ("cline", "outside"), ("cline", "review_write"), ("gemini", "denied")
])
def test_inbox_cannot_elevate_role(tmp_path, monkeypatch, engine, scenario):
    inbox = Answers()
    assert run_fixture(tmp_path, monkeypatch, engine, scenario, inbox=inbox).returncode
    assert not inbox.requests


def test_permission_does_not_persist_secret_hidden_by_json_quoting(tmp_path, monkeypatch):
    inbox = Answers()
    result = run_fixture(tmp_path, monkeypatch, scenario="secret_edit", edit=True, inbox=inbox)
    assert result.returncode == 1 and "cannot be recorded in full" in result.message
    assert not inbox.requests


@pytest.mark.parametrize("scenario", ["inbox_wrong_id", "inbox_no_ack", "inbox_mixed", "inbox_rejected_success",
                                      "inbox_repeat"])
def test_interactive_failures(tmp_path, monkeypatch, scenario):
    inbox = Answers("reject")
    result = run_fixture(tmp_path, monkeypatch, scenario=scenario, edit=True, inbox=inbox)
    assert result.returncode and ANSWER not in result.message
    if scenario in ("inbox_wrong_id", "inbox_no_ack"):
        assert inbox.sent == [1] and not inbox.receipts
    if scenario == "inbox_mixed":
        assert not inbox.requests
    if scenario == "inbox_repeat":
        assert len(inbox.requests) == 8 and inbox.receipts == inbox.sent


def test_dead_session_cancels_waiting_question(tmp_path, monkeypatch):
    class Waiting(Answers):
        async def ask(self, kind, prompt, session):
            await asyncio.Future()

    inbox = Waiting()
    result = run_fixture(tmp_path, monkeypatch, scenario="inbox_dead", inbox=inbox)
    assert result.returncode == 1 and "session ended" in result.message
    assert not inbox.sent


def serve_inbox(engine, scenario, req, receive, send, update):
    text = req["params"]["prompt"][0]["text"]
    assert "AGENT_OFFICE_QUESTION" in text and "AGENT_OFFICE_FINAL" in text
    question = "AGENT_OFFICE_QUESTION\n어느 값인가요?"
    if scenario == "inbox_mixed":
        question += "\nAGENT_OFFICE_FINAL"
    for turn in range(9 if scenario == "inbox_repeat" else 1):
        update("agent_message_chunk", content={"type": "text", "text": question})
        send(id=req["id"], result={"stopReason": "end_turn"})
        if scenario in ("inbox_mixed", "inbox_dead") or turn == 8:
            return
        req = receive("session/prompt")
        assert req["id"] == 5 + turn and req["params"]["sessionId"] == "fixture"
        text = req["params"]["prompt"][0]["text"]
        assert text.endswith("사용자 답변\n& |") and PROMPT not in text
    if scenario == "inbox_no_ack":
        return
    if scenario == "inbox_wrong_id":
        send(id=4, result={"stopReason": "end_turn"})
        return
    if engine == "cline":
        target = Path.cwd() / "target"
        call = editor_call(target)
        update("tool_call_update", status="pending", **call)
        send(method="session/request_permission", id="permission-6", params={"sessionId": "fixture",
             "toolCall": call, "options": [{"kind": k, "optionId": k} for k in ("allow_once", "reject_once")]})
        response = json.loads(sys.stdin.readline())
        assert response["id"] == "permission-6"
        if response["result"]["outcome"] == {"outcome": "selected", "optionId": "allow_once"}:
            update("tool_call_update", toolCallId="op", status="in_progress")
            target.write_text("edited")
            update("tool_call_update", toolCallId="op", status="completed", rawOutput={"success": True})
        else:
            assert response["result"]["outcome"] == {"outcome": "selected", "optionId": "reject_once"}
            update("tool_call_update", toolCallId="op", status="failed")
            update("tool_call", status="pending", **call)
            update("tool_call_update", toolCallId="op", status="failed", rawOutput="User rejected the tool call")
            if scenario == "inbox_rejected_success":
                update("tool_call_update", toolCallId="op", status="completed", rawOutput={"success": True})
    update("agent_message_chunk", content={"type": "text", "text": "finished\nAGENT_OFFICE_FINAL"})
    send(id=req["id"], result={"stopReason": "end_turn"})


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
    if scenario.startswith("inbox_"):
        return serve_inbox(engine, scenario, req, receive, send, update)
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
        if scenario == "secret_edit":
            call["rawInput"]["new_text"] = '"password":"example_synthetic"'
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
