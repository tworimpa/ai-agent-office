"""Local fixture coding turns for the three-worktree desktop journey."""
import json
from pathlib import Path
import sys


QUESTION = "기존 설정을 새 형식으로 자동 변환할까요?"
ANSWER = "자동 변환합니다"
REGRESSION = '''from settings import reopen

def test_windows_legacy_theme_survives_reopen(tmp_path):
    path = tmp_path / "AppData" / "Local" / "Fixture" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'\\xef\\xbb\\xbf{\\r\\n  "theme": "dark"\\r\\n}\\r\\n')
    assert reopen(path) == "dark"
'''
CONVERT = 'return {"appearance": {"theme": data["theme"]}} if "theme" in data else data'
STATUS = "#43 · 후보 검증 완료 · 사용자 검토 대기"


def populate(root):
    (root / "settings.py").write_text('''import json

def convert(data):
    return data

def reopen(path):
    if not path.exists():
        return "light"
    data = json.loads(path.read_bytes().decode("utf-8-sig"))
    return data.get("appearance", {}).get("theme", "light")
''', encoding="utf8")
    (root / "tests/test_settings.py").write_text('''from settings import reopen

def test_current_theme_survives_reopen(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"appearance":{"theme":"dark"}}', encoding="utf8")
    assert reopen(path) == "dark"
''', encoding="utf8")
    (root / "tests/test_defaults.py").write_text('''from settings import reopen

def test_missing_settings_use_default(tmp_path):
    assert reopen(tmp_path / "missing.json") == "light"
''', encoding="utf8")
    (root / "status_comment.py").write_text('''def render(issue, state):
    labels = {"checking": "검증 중", "ready": "완료"}
    return f"#{issue} · {labels[state]}"
''', encoding="utf8")



def respond(initial, prompt, editing, turn, send):
    if not editing:
        return "VERDICT: APPROVED\n독립된 작업 트리에서 요청을 확인했습니다."
    if "#42" in initial:
        if "add only a new pytest regression file" in initial:
            Path("tests/test_windows_restore.py").write_text(REGRESSION, encoding="utf8")
            return "Windows에서 저장한 기존 설정 파일을 다시 열면 어두운 테마가 밝게 돌아가는 회귀 테스트를 추가했습니다."
        if ANSWER not in prompt:
            return "AGENT_OFFICE_PRODUCT_DECISION\n" + QUESTION
        path = Path("settings.py")
        text = path.read_text(encoding="utf8").replace("return data\n", CONVERT + "\n", 1)
        text = text.replace('return data.get("appearance", {}).get("theme", "light")', '''normalized = convert(data)
    if normalized != data:
        path.write_text(json.dumps(normalized), encoding="utf8")
    return normalized.get("appearance", {}).get("theme", "light")''')
        path.write_text(text, encoding="utf8")
        return "기존 설정을 자동 변환하고 다시 열 때 저장해 둔 테마를 복원합니다."
    if "#43" in initial:
        path = Path("status_comment.py")
        path.write_text(path.read_text(encoding="utf8").replace('"ready": "완료"', '"ready": "후보 검증 완료 · 사용자 검토 대기"'),
                        encoding="utf8")
        Path("tests/test_status_comment.py").write_text('from status_comment import render\n\n'
            f'def test_review_status_comment():\n    assert render(43, "ready") == {STATUS!r}\n'
            '    assert render(43, "checking") == "#43 · 검증 중"\n', encoding="utf8")
        return "GitHub 상태 댓글에 검증 완료와 사용자 검토 대기를 표시하고, 검증 중 문구도 확인했습니다."
    if "#41" in initial:
        if ANSWER not in prompt:
            return "AGENT_OFFICE_PRODUCT_DECISION\n" + QUESTION
        target = Path("settings.py")
        scope = {"threadId": "settings-fixture", "turnId": turn}
        patch = {"type": "fileChange", "id": "convert-patch", "changes": [
            {"path": str(target.resolve()), "kind": {"type": "update", "move_path": None},
             "diff": "-    return data\n+    " + CONVERT}]}
        send({"method": "item/started", "params": {**scope, "item": {**patch, "status": "inProgress"}}})
        send({"id": 71, "method": "item/fileChange/requestApproval", "params": {**scope, "itemId": patch["id"]}})
        decision = json.loads(sys.stdin.buffer.readline())
        assert decision["id"] == 71 and decision["result"]["decision"] == "accept"
        send({"method": "serverRequest/resolved", "params": {"threadId": "settings-fixture", "requestId": 71}})
        send({"method": "item/completed", "params": {**scope, "item": {**patch, "status": "completed"}}})
        target.write_text(target.read_text(encoding="utf8").replace("return data\n", CONVERT + "\n", 1), encoding="utf8")
        return "기존 설정을 새 형식으로 변환했습니다."
    raise AssertionError("A queued fixture task unexpectedly reached the coding provider")


if __name__ == "__main__":
    populate(Path(sys.argv[1]))
