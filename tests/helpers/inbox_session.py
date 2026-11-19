"""Synthetic Cline ACP session: ask once, then wait for an actual one-use tool decision."""
import json
import os
from pathlib import Path
import sys


def emit(**msg):
    print(json.dumps({"jsonrpc": "2.0", **msg}, ensure_ascii=False), flush=True)


def update(session, **event):
    emit(method="session/update", params={"sessionId": session, "update": event})


def serve():
    sid = "inbox-fixture-session"
    requested = False
    mode = "plan"
    for line in sys.stdin:
        req = json.loads(line)
        method = req["method"]
        if method == "initialize":
            result = {"protocolVersion": 1, "agentInfo": {"name": "cline", "version": "3.0.74"}}
        elif method == "session/new":
            result = {"sessionId": sid, "configOptions": [{"id": "auto_approve", "currentValue": False}]}
        elif method == "session/set_mode":
            mode = req["params"]["modeId"]
            result = {}
        elif method == "session/prompt":
            assert req["params"]["sessionId"] == sid
            prompt = req["params"]["prompt"][0]["text"]
            if not requested:
                requested = True
                text = "AGENT_OFFICE_QUESTION\n" + os.environ.get("OFFICE_FIXTURE_QUESTION", "어느 규격을 적용할까요?")
            else:
                assert os.environ.get("OFFICE_FIXTURE_ANSWER", "use spec v2") in prompt
                source = os.environ.get("OFFICE_FIXTURE_SOURCE")
                if source:
                    assert ("verified delegated specialist's" in prompt) == (source == "delegate")
                writing = mode == "act"
                name = "editor" if writing else "read_files"
                path = str(Path("web/value.txt").resolve())
                raw = {"path": path, "new_text": "new"} if writing else {"files": [{"path": path}]}
                call = {"toolCallId": "file-1", "kind": "edit" if writing else "read",
                        "title": name + ": " + path, "rawInput": raw}
                emit(id="permission-1", method="session/request_permission",
                     params={"sessionId": sid, "toolCall": call, "options": [
                         {"kind": kind, "optionId": kind} for kind in ("allow_once", "reject_once")]})
                decision = json.loads(sys.stdin.readline())
                assert decision["id"] == "permission-1"
                allowed = decision["result"]["outcome"]["optionId"] == "allow_once"
                if allowed and writing:
                    Path(path).write_text("new")
                with Path(os.environ["OFFICE_INBOX_TRACE"]).open("a", encoding="utf-8") as trace:
                    trace.write(f"{os.getpid()} {sid} {allowed}\n")
                update(sid, sessionUpdate="tool_call_update", toolCallId="file-1",
                       status="completed" if allowed else "failed", rawOutput={"success": allowed})
                text = ("applied" if allowed else "continued after refusal") + "\nAGENT_OFFICE_FINAL"
            update(sid, sessionUpdate="agent_message_chunk", content={"type": "text", "text": text})
            result = {"stopReason": "end_turn"}
        else:
            raise AssertionError(method)
        emit(id=req["id"], result=result)


if __name__ == "__main__":
    if "--version" in sys.argv:
        print("3.0.74")
    elif "--help" in sys.argv:
        print("--acp --auto-approve")
    else:
        serve()
