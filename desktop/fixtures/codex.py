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


def meeting():
    prompt = sys.stdin.buffer.read().decode("utf-8")
    stage = prompt.splitlines()[0]
    assert stage in ("MEETING_DEVELOPER", "MEETING_REVIEWER")
    material = json.loads(prompt.splitlines()[-1])
    assert "tests/test_local.py" in material["evidence"]["excerpts"]
    if stage == "MEETING_REVIEWER":
        assert material["previous"]["opinion"]
    goal = material["request"]
    simple = "작업 파일을 설명해 주세요" in goal
    revised = "Operator direction for developer" in goal
    if revised:
        assert "desktop verified" in goal and "수정한 목표" in goal
    plan = {
        "outcome": "파일과 테스트를 설명" if simple else "지정 문구 및 테스트 확인",
        "scope": ["note.txt", "tests/test_local.py"],
        "tasks": [{"id": "T1", "owner": "developer", "description": "파일 확인" if simple else "문구 변경",
                   "depends_on": []},
                  {"id": "T2", "owner": "reviewer", "description": "테스트 근거 검토", "depends_on": ["T1"]}],
        "rationale": ["read_note.py가 note.txt를 읽고 tests/test_local.py가 해당 내용을 검증한다."],
        "criteria": [{"criterion": "대상 문구가 저장된다.", "verification": "단위 테스트 실행",
                      "test_files": ["tests/test_local.py"]},
                     {"criterion": "운영자가 문구를 확인했다.", "verification": "운영자 육안 확인"}],
        "questions": [] if simple else [
            {"question": "문장 형식은?", "options": ["한 줄", "두 줄"]},
            {"question": "마지막 줄바꿈은?", "options": ["유지", "제거"]},
        ],
    }
    if revised:
        plan["outcome"] = "수정한 목표와 추가 지시 반영"
        plan["tasks"][0]["description"] = "note.txt 한 줄을 desktop verified로 변경하고 줄바꿈 유지"
        plan["rationale"].append("추가 지시의 문구를 적용한 뒤 지정 테스트를 다시 실행한다.")
    result = {"opinion": stage + " 기존 코드와 테스트를 확인했습니다.", "issues": [], "plan": plan}
    emit(type="turn.started")
    emit(type="item.completed", item={"type": "agent_message", "text": json.dumps(result, ensure_ascii=False)})
    emit(type="turn.completed")


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
        if not editing and "작업 파일을 설명해 주세요" in prompt:
            result = "read_note.py가 note.txt를 읽습니다."
        elif editing and turns == 1:
            assert "수정한 목표" in prompt and "한 줄" in prompt
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
    case ["exec", "--json", "--sandbox", "read-only", "-"]:
        meeting()
    case ["app-server", "--listen", "stdio://"]:
        app_server()
    case _:
        raise SystemExit(2)
