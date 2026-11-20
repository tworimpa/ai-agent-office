"""Local app-server fixture for the packaged desktop verification."""
import json
import os
from pathlib import Path
import sys


def read():
    line = sys.stdin.buffer.readline()
    return json.loads(line) if line else None


def emit(**record):
    print(json.dumps(record, ensure_ascii=True), flush=True)


def response(request, **data):
    emit(id=request["id"], result=data)


def app_server():
    response(read(), userAgent="desktop fixture")
    assert read()["method"] == "initialized"
    request = read()
    assert request["method"] == "thread/start"
    editing = request["params"]["sandbox"] == "workspace-write"
    sandbox = {"type": "workspaceWrite", "excludeTmpdirEnvVar": True, "excludeSlashTmp": True} if editing else {"type": "readOnly"}
    response(request, thread={"id": "desktop-fixture"}, cwd=str(Path.cwd()), approvalsReviewer="user",
             approvalPolicy="on-request", sandbox=sandbox)
    turns = 0
    while request := read():
        if request["method"] == "thread/inject_items":
            response(request)
            continue
        assert request["method"] == "turn/start"
        turns += 1
        turn = str(turns)
        response(request, turn={"id": turn, "status": "inProgress"})
        prompt = request["params"]["input"][0]["text"]
        if editing and turns == 1:
            result = "AGENT_OFFICE_QUESTION\n문서에 변경을 추가할까요?"
        elif editing and turns == 2:
            result = "추가 지시대로 반영하겠습니다."
        elif editing:
            assert "desktop verified" in prompt
            Path("note.txt").write_text("desktop verified\n", encoding="utf-8", newline="\n")
            result = "desktop verified 문구를 반영했습니다."
        else:
            assert "Review the requested changes" in prompt
            assert "1 passed" in prompt
            assert Path("note.txt").read_text(encoding="utf-8") == "desktop verified\n"
            result = "VERDICT: APPROVED\n대상 문구와 지정 테스트를 확인했습니다."
        item = {"id": "reply-" + turn, "type": "agentMessage", "phase": "final_answer", "text": result}
        for phase in ("started", "completed"):
            emit(method="item/" + phase, params={"threadId": "desktop-fixture", "turnId": turn, "item": item})
        emit(method="turn/completed", params={"threadId": "desktop-fixture", "turn": {"id": turn, "status": "completed"}})


arguments = sys.argv[1:]
if os.name == "nt" and os.environ.get("AGENT_OFFICE_DESKTOP_LOCAL") == "1" and "app-server" in arguments:
    assert arguments[:2] == ["-c", "windows.sandbox=mxc"]
    arguments = arguments[2:]

match arguments:
    case ["--version"]:
        print("codex-cli 0.169.1")
    case ["login", "status"]:
        print("Logged in using fixture")
    case ["exec", "--help"]:
        print("--json --sandbox")
    case ["app-server", "--listen", "stdio://"]:
        app_server()
    case _:
        raise SystemExit(2)
