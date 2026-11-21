"""Local app-server fixture for the packaged desktop verification."""
import json
import os
from pathlib import Path
import sys

# The packaged Studio sets PYTHONSAFEPATH when spawning its test CLIs.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from meeting import meeting_result


def read():
    line = sys.stdin.buffer.readline()
    return json.loads(line) if line else None


def emit(**record):
    print(json.dumps(record, ensure_ascii=True), flush=True)


def response(request, **data):
    emit(id=request["id"], result=data)


def meeting():
    result = meeting_result(sys.stdin.buffer.read().decode("utf-8"))
    emit(type="turn.started")
    emit(type="item.completed", item={"type": "agent_message", "text": json.dumps(result, ensure_ascii=False)})
    emit(type="turn.completed", usage={"input_tokens": 70, "cached_input_tokens": 10, "output_tokens": 30})


def metadata(request):
    if request["method"] == "account/rateLimits/read":
        response(request, accountId="desktop-fixture-account", ordinaryUsageAllowed=True,
                 rateLimits={"limitId": "codex", "primary": {"usedPercent": 20, "windowDurationMins": 300}})
    elif request["method"] == "model/list":
        response(request, data=[{"id": "desktop-fixture", "model": "desktop-fixture", "displayName": "Desktop fixture",
                 "hidden": False, "isDefault": True, "defaultReasoningEffort": "medium",
                 "supportedReasoningEfforts": [{"reasoningEffort": effort} for effort in ("low", "medium", "high")]}],
                 nextCursor=None)
    else:
        return False
    return True


def app_server():
    response(read(), userAgent="desktop fixture")
    assert read()["method"] == "initialized"
    request = read()
    while request and metadata(request):
        request = read()
    if request is None:
        return
    assert request["method"] == "thread/start"
    editing = request["params"]["sandbox"] == "workspace-write"
    sandbox = {"type": "workspaceWrite", "excludeTmpdirEnvVar": True, "excludeSlashTmp": True} if editing else {"type": "readOnly"}
    response(request, thread={"id": "desktop-fixture"}, cwd=str(Path.cwd()), approvalsReviewer="user",
             approvalPolicy="on-request", sandbox=sandbox, modelProvider="openai",
             model=request["params"].get("model", "desktop-fixture"),
             reasoningEffort=request["params"]["config"].get("model_reasoning_effort", "medium"))
    turns = 0
    while request := read():
        if metadata(request):
            continue
        if request["method"] == "thread/inject_items":
            response(request)
            continue
        assert request["method"] == "turn/start"
        turns += 1
        turn = str(turns)
        response(request, turn={"id": turn, "status": "inProgress"})
        prompt = request["params"]["input"][0]["text"]
        if not editing and turns == 1 and "작업 파일을 설명해 주세요" in prompt:
            result = "AGENT_OFFICE_QUESTION\n테스트도 설명할까요?"
        elif not editing and turns > 1:
            result = "read_note.py가 note.txt를 읽습니다."
        elif editing and turns == 1:
            assert "수정한 목표" in prompt and "한 줄" in prompt
            result = "AGENT_OFFICE_QUESTION\n문서에 변경을 추가할까요?"
        elif editing and turns == 2:
            result = "추가 지시대로 반영하겠습니다."
        elif editing:
            assert "desktop verified" in prompt
            target = Path.cwd() / "note.txt"
            patch = {"type": "fileChange", "id": "note-patch", "changes": [
                {"path": str(target), "kind": {"type": "update", "move_path": None},
                 "diff": "-original\n+desktop verified\n"}]}
            scope = {"threadId": "desktop-fixture", "turnId": turn}
            emit(method="item/started", params={**scope, "item": {**patch, "status": "inProgress"}})
            emit(id=71, method="item/fileChange/requestApproval", params={**scope, "itemId": patch["id"]})
            answer = read()
            assert answer["id"] == 71 and answer["result"]["decision"] == "accept"
            emit(method="serverRequest/resolved", params={"threadId": "desktop-fixture", "requestId": 71})
            emit(method="item/completed", params={**scope, "item": {**patch, "status": "completed"}})
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
        emit(method="thread/tokenUsage/updated", params={"threadId": "desktop-fixture", "turnId": turn, "tokenUsage": {
            "total": {"inputTokens": 70 * turns, "outputTokens": 30 * turns, "totalTokens": 100 * turns},
            "last": {"totalTokens": 100}, "modelContextWindow": 4000}})
        emit(method="turn/completed", params={"threadId": "desktop-fixture", "turn": {"id": turn, "status": "completed"}})


arguments = sys.argv[1:]
if os.name == "nt" and os.environ.get("AGENT_OFFICE_DESKTOP_LOCAL") == "1" and (
        "app-server" in arguments or arguments[:1] == ["-c"]):
    assert arguments[:2] == ["-c", "windows.sandbox=mxc"]
    arguments = arguments[2:]

match arguments:
    case ["--version"]:
        print("codex-cli 0.169.1")
    case ["login", "status"]:
        print("Logged in using fixture")
    case ["exec", "--help"]:
        print("--json --sandbox")
    case ["exec", "--json", "--sandbox", "read-only", *selection, "-"]:
        if selection[:2] == ["--model", "desktop-fixture"]:
            selection = selection[2:]
        assert not selection or (len(selection) == 2 and selection[0] == "-c" and
                                 selection[1] in {"model_reasoning_effort=" + e for e in ("low", "medium", "high")})
        meeting()
    case ["app-server", "--listen", "stdio://"]:
        app_server()
    case _:
        raise SystemExit(2)
