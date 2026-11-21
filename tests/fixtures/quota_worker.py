"""Synthetic public Codex protocol with externally controlled quota and a countable edit."""
import json
import os
from pathlib import Path
import sys


def send(value):
    print(json.dumps(value), flush=True)


root = Path(os.environ["OFFICE_FIXTURE_QUOTA"])
for raw in sys.stdin:
    message = json.loads(raw)
    method = message.get("method")
    if method == "initialized":
        continue
    if method == "initialize":
        result = {"userAgent":"fixture"}
    elif method == "account/rateLimits/read":
        allowed = (root / "grant").exists()
        result = {"accountId":"fixture-personal-account","ordinaryUsageAllowed":allowed,
                  "rateLimits":{"limitId":"codex","primary":{"usedPercent":1 if allowed else 100,
                                                            "windowDurationMins":300,"resetsAt":1}}}
    elif method == "thread/start":
        edit = message["params"]["sandbox"] == "workspace-write"
        sandbox = ({"type":"workspaceWrite","excludeTmpdirEnvVar":True,"excludeSlashTmp":True}
                   if edit else {"type":"readOnly"})
        result = {"thread":{"id":"fixture"},"model":"fixture-model","modelProvider":"openai",
                  "reasoningEffort":"medium","cwd":str(Path.cwd()),"approvalsReviewer":"user",
                  "approvalPolicy":"on-request","sandbox":sandbox}
    elif method == "thread/inject_items":
        result = {}
    elif method == "turn/start":
        send({"id":message["id"],"result":{"turn":{"id":"turn","status":"inProgress"}}})
        with (root / "turns").open("a",encoding="utf8") as stream:
            stream.write(("edit" if edit else "review")+"\n")
        if edit:
            Path("result.py").write_text("value = 1\n")
        common = {"threadId":"fixture","turnId":"turn"}
        send({"method":"thread/tokenUsage/updated","params":dict(common,tokenUsage={
            "total":{"totalTokens":17,"inputTokens":12,"outputTokens":5},
            "last":{"totalTokens":11},"modelContextWindow":1000})})
        item = {"id":"out","type":"agentMessage","phase":"final_answer",
                "text":("Implemented." if edit else "VERDICT: APPROVED")+"\nAGENT_OFFICE_COMPLETE"}
        for phase in ("started","completed"):
            send({"method":"item/"+phase,"params":dict(common,item=item)})
        send({"method":"turn/completed","params":{"threadId":"fixture","turn":{"id":"turn","status":"completed"}}})
        continue
    else:
        send({"id":message["id"],"error":{"code":-32601,"message":"Unsupported method."}})
        continue
    send({"id":message["id"],"result":result})
