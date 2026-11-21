"""Local fixture coding turns for the three-worktree desktop journey."""
import json
from pathlib import Path
import sys


QUESTION = "기존 설정을 새 형식으로 자동 변환할까요?"
ANSWER = "자동 변환합니다"
REGRESSION = """from settings import valid

def test_invalid_legacy_setting():
    assert valid({"legacy": None}) is False
"""


def respond(initial, prompt, editing, turn, send):
    if not editing:
        return "VERDICT: APPROVED\n독립된 작업 트리에서 요청을 확인했습니다."
    if "#42" in initial:
        if "add only a new pytest regression file" in initial:
            Path("tests/test_invalid_legacy.py").write_text(REGRESSION, encoding="utf8")
            return "잘못된 이전 설정을 넘겨 원본의 오류를 재현하는 테스트를 추가했습니다."
        if ANSWER not in prompt:
            return "AGENT_OFFICE_PRODUCT_DECISION\n" + QUESTION
        path = Path("settings.py")
        path.write_text(path.read_text(encoding="utf8").replace(
            "return True", 'return data.get("legacy") is not None if "legacy" in data else True'), encoding="utf8")
        return "자동 변환 중 잘못된 이전 설정 값을 거절하도록 검증했습니다."
    if "#43" in initial:
        Path("release.md").write_text("이전 설정 복원에 대한 릴리스 노트 초안입니다.\n", encoding="utf8")
        return "릴리스 노트 초안을 작성했습니다."
    if "#51" in initial:
        return "AGENT_OFFICE_PRODUCT_DECISION\n동시 표시 검증에 사용할 설정 항목은 무엇인가요?"
    if "#41" in initial:
        if ANSWER not in prompt:
            return "AGENT_OFFICE_PRODUCT_DECISION\n" + QUESTION
        target = Path("settings.py")
        scope = {"threadId": "settings-fixture", "turnId": turn}
        patch = {"type": "fileChange", "id": "convert-patch", "changes": [
            {"path": str(target.resolve()), "kind": {"type": "update", "move_path": None},
             "diff": '-    return data\n+    return {"value": data["legacy"]} if "legacy" in data else data'}]}
        send({"method": "item/started", "params": {**scope, "item": {**patch, "status": "inProgress"}}})
        send({"id": 71, "method": "item/fileChange/requestApproval", "params": {**scope, "itemId": patch["id"]}})
        decision = json.loads(sys.stdin.buffer.readline())
        assert decision["id"] == 71 and decision["result"]["decision"] == "accept"
        send({"method": "serverRequest/resolved", "params": {"threadId": "settings-fixture", "requestId": 71}})
        send({"method": "item/completed", "params": {**scope, "item": {**patch, "status": "completed"}}})
        target.write_text(target.read_text(encoding="utf8").replace(
            "return data", 'return {"value": data["legacy"]} if "legacy" in data else data'), encoding="utf8")
        return "기존 설정을 새 형식으로 변환했습니다."
    Path("release.md").write_text("추가 확인을 마쳤습니다.\n", encoding="utf8")
    return "추가 확인을 마쳤습니다."
