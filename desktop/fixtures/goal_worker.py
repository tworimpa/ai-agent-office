"""Deterministic coding CLI for the installed settings-recovery journey."""
import json
from pathlib import Path
import sys


def send(value):
    print(json.dumps(value), flush=True)


def plan():
    return {"opinion": "설정 저장 및 복원을 확인했습니다.", "issues": [], "plan": {
        "outcome": "작업실을 다시 열어도 저장된 설정을 복원한다.",
        "scope": ["settings.py", "tests/test_settings.py"],
        "tasks": [{"id": "T1", "owner": "developer", "description": "저장한 설정 복원", "depends_on": []},
                  {"id": "T2", "owner": "reviewer", "description": "전체 회귀 검증", "depends_on": ["T1"]}],
        "criteria": [{"criterion": "다시 열면 마지막 선택이 유지된다.", "verification": "재시작 회귀",
                      "test_files": ["tests/test_settings.py"]}],
        "rationale": ["설정 복원 함수와 기존 테스트를 확인했습니다."],
        "questions": [{"question": "저장된 테마가 없을 때 기본 테마는?", "options": ["밝게", "어둡게"]}],
    }}


def develop():
    path = Path("settings.py")
    source = path.read_text(encoding="utf8")
    if 'return "light"' in source:
        source = source.replace('return "light"', 'return json.loads(path.read_text(encoding="utf8"))["theme"]')
        hypothesis = "저장 파일을 읽지 않아 설정이 사라졌습니다."
    else:
        source = source.replace('return json.loads(path.read_text(encoding="utf8"))["theme"]',
                                'return json.loads(path.read_text(encoding="utf8"))["theme"] if path.exists() else "light"')
        hypothesis = "처음 실행할 때에는 저장 파일이 없어 기본 테마가 필요합니다."
    path.write_text(source, encoding="utf8")
    return hypothesis


def server():
    editing = False
    turns = 0
    for line in sys.stdin.buffer:
        request = json.loads(line)
        method = request.get("method")
        if method == "initialized":
            continue
        if method == "initialize":
            result = {"userAgent": "goal-fixture"}
        elif method == "thread/start":
            editing = request["params"]["sandbox"] == "workspace-write"
            sandbox = {"type": "workspaceWrite", "excludeTmpdirEnvVar": True, "excludeSlashTmp": True} if editing else {"type": "readOnly"}
            result = {"thread": {"id": "settings-fixture"}, "cwd": str(Path.cwd()), "approvalsReviewer": "user",
                      "approvalPolicy": "on-request", "sandbox": sandbox}
        elif method == "thread/inject_items":
            result = {}
        elif method == "turn/start":
            turns += 1
            turn = str(turns)
            send({"id": request["id"], "result": {"turn": {"id": turn, "status": "inProgress"}}})
            prompt = request["params"]["input"][0]["text"]
            if editing and turns == 1 and 'return "light"' in Path("settings.py").read_text(encoding="utf8") and "밝게" not in prompt:
                text = "AGENT_OFFICE_QUESTION\n저장된 테마가 없을 때 기본 테마는 밝게와 어둡게 중 무엇인가요?"
            else:
                if editing and turns > 1:
                    assert "밝게" in prompt
                text = develop() if editing else "VERDICT: APPROVED\n설정 복원 경로를 검토했습니다."
            item = {"id": "reply-" + turn, "type": "agentMessage", "phase": "final_answer", "text": text}
            for phase in ("started", "completed"):
                send({"method": "item/" + phase, "params": {"threadId": "settings-fixture", "turnId": turn, "item": item}})
            send({"method": "turn/completed", "params": {"threadId": "settings-fixture", "turn": {"id": turn, "status": "completed"}}})
            continue
        else:
            raise ValueError(method)
        send({"id": request["id"], "result": result})


engine, *args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 0.169.1" if engine == "codex" else "agy 1.0.0-fixture")
elif args == ["login", "status"]:
    print("Logged in using fixture")
elif "--help" in args:
    print("--json --sandbox --input-format --output-format --mode --disable-slash-commands --print-timeout")
elif "app-server" in args and engine == "codex":
    server()
elif engine == "codex" and "exec" in args:
    prompt = sys.stdin.buffer.read().decode("utf8")
    text = json.dumps(plan(), ensure_ascii=False) if prompt.startswith("MEETING_") else develop() if "workspace-write" in args else "VERDICT: APPROVED"
    send({"type": "turn.started"})
    send({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
    send({"type": "turn.completed"})
elif engine == "agy":
    prompt = json.load(sys.stdin)["message"]["content"]
    text = json.dumps(plan(), ensure_ascii=False) if prompt.startswith("MEETING_") else "VERDICT: APPROVED\n설정 복원 경로를 검토했습니다."
    send({"event": "result", "result": {"status": "SUCCESS", "response": text, "conversation_id": "settings-fixture"}})
else:
    raise SystemExit(2)
