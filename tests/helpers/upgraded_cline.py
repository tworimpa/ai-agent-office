"""Synthetic ACP CLI exercising the ordinary read and independent consultation paths."""
import json
from pathlib import Path
import sys


def send(**fields):
    print(json.dumps({"jsonrpc":"2.0", **fields}), flush=True)


if "--version" in sys.argv:
    print("3.0.75")
    sys.exit()
for line in sys.stdin:
    req = json.loads(line)
    method = req["method"]
    if method == "initialize":
        result = {"protocolVersion":1, "agentInfo":{"name":"cline", "version":"3.0.75"}}
    elif method == "session/new":
        result = {"sessionId":"test", "configOptions":[{"id":"auto_approve", "currentValue":False}],
                  "modes":{"availableModes":[{"id":"plan"}]}}
    elif method == "session/set_mode":
        result = {}
    else:
        assert method == "session/prompt"
        prompt = req["params"]["prompt"][0]["text"]
        away = prompt.startswith("AWAY_")
        path = Path("evidence.json" if away else "reference.txt").resolve()
        send(id=100, method="session/request_permission", params={"sessionId":"test",
             "toolCall":{"toolCallId":"read", "title":"read_files: fixture", "kind":"read",
                         "rawInput":{"files":[{"path":str(path)}]}},
             "options":[{"optionId":"allow_once", "kind":"allow_once"}]})
        assert json.loads(sys.stdin.readline())["result"]["outcome"]["optionId"] == "allow_once"
        answer = path.read_text(encoding="utf-8").strip()
        if away:
            item = json.loads(answer)["evidence"][0]
            response = {**item, "answer":item["quote"]}
            if prompt.startswith("AWAY_VERIFIER"):
                response["verdict"] = "verified"
            answer = json.dumps(response)
        for update in [
            {"sessionUpdate":"tool_call_update", "toolCallId":"read", "status":"completed", "rawOutput":{"success":True}},
            {"sessionUpdate":"agent_message_chunk", "content":{"type":"text","text":answer+"\nAGENT_OFFICE_FINAL"}},
        ]:
            send(method="session/update", params={"sessionId":"test","update":update})
        result = {"stopReason":"end_turn"}
    send(id=req["id"], result=result)
