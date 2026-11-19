"""Two protocol-speaking test CLI programs for studio host/role integration."""
import json
import os
from pathlib import Path
import sys


def emit(data):
    print(json.dumps(data, ensure_ascii=False), flush=True)


def answer(engine, prompt):
    role = "review" if "VERDICT: APPROVED" in prompt else "develop"
    if engine == "cline":
        assert "web/value.txt" in prompt and "contracts/wanted.txt" in prompt
    with Path(os.environ["OFFICE_FIXTURE_CALLS"]).open("a", encoding="utf-8") as f:
        f.write(engine + " " + role + "\n")
    if role == "review":
        assert Path("web/value.txt").read_text() == "new"
        return "VERDICT: APPROVED\n" + engine + " fixture review"
    assert "contracts" in prompt and "web" in prompt
    Path("web/value.txt").write_text(Path("contracts/wanted.txt").read_text())
    return engine + " fixture edit"


def run_acp(engine):
    edit_mode = "act" if engine == "cline" else "autoEdit"
    for line in sys.stdin:
        req = json.loads(line)
        method = req.get("method")
        if method == "initialize":
            result = {"protocolVersion": 1, "agentInfo": {"name": engine, "version": "3.0.74" if engine == "cline" else "0.69.0"},
                      "agentCapabilities": {"loadSession": False}}
        elif method == "session/new":
            result = {"sessionId": "fixture-session", "modes": {"currentModeId": "plan", "availableModes": [
                {"id": "plan", "name": "Plan"}, {"id": edit_mode, "name": "Edit"}]},
                "configOptions": [{"id": "auto_approve", "currentValue": False}]}
        elif method == "session/set_mode":
            result = {}
        elif method == "session/prompt":
            prompt = req["params"]["prompt"][0]["text"]
            if engine == "cline" and "VERDICT: APPROVED" not in prompt:
                path = str(Path("web/value.txt").resolve())
                emit({"jsonrpc": "2.0", "id": "edit-approval", "method": "session/request_permission",
                      "params": {"sessionId": "fixture-session", "options": [
                          {"kind": "allow_once", "optionId": "allow_once"},
                          {"kind": "reject_once", "optionId": "reject_once"}],
                          "toolCall": {"toolCallId": "edit", "kind": "edit", "title": "editor: " + path,
                                       "rawInput": {"path": path, "new_text": "new"}}}})
                decision = json.loads(sys.stdin.readline())
                assert decision["id"] == "edit-approval" and decision["result"]["outcome"]["optionId"] == "allow_once"
                emit({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "fixture-session",
                      "update": {"sessionUpdate": "tool_call_update", "toolCallId": "edit",
                                 "status": "completed", "rawOutput": {"success": True}}}})
            text = answer(engine, prompt) + "\nAGENT_OFFICE_FINAL"
            emit({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "fixture-session",
                  "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}}})
            result = {"stopReason": "end_turn"}
        else:
            raise AssertionError(method)
        emit({"jsonrpc": "2.0", "id": req["id"], "result": result})


if __name__ == "__main__":
    name = sys.argv[1]
    if "--version" in sys.argv:
        print("3.0.74" if name == "cline" else "0.69.0")
    elif "--help" in sys.argv:
        print("--acp --auto-approve --approval-mode plan auto_edit")
    elif name in ("gemini", "cline"):
        run_acp(name)
    else:
        raise AssertionError(name)
