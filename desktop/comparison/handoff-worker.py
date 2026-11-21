"""Comparison-specific turns for the existing goal_worker app-server fixture."""
from pathlib import Path


def respond(initial, prompt, editing, turn, send):
    if not editing:
        return "VERDICT: APPROVED\n독립 검토를 마쳤습니다."
    target = Path("settings.py")
    assert Path("briefing-fixture.txt").is_file() and "restore" in target.read_text(encoding="utf8")
    controls = Path(__file__).resolve().parent
    if (controls/"quota-closed").is_file() and turn == "1":
        assert (controls/"quota-grant").is_file()
        target.write_text('import json\n\ndef restore(path):\n'
                          '    return json.loads(path.read_text(encoding="utf8"))["theme"] if path.exists() else "light"\n',
                          encoding="utf8")
        with (controls/"quota-edits").open("a",encoding="utf8") as output:
            output.write(str(Path.cwd())+"\n")
        return "저장 파일 복원을 수정했습니다."
    if turn == "1":
        return "AGENT_OFFICE_PRODUCT_DECISION\n저장 파일이 없으면 어떤 테마로 열까요?"
    if "Operator directions (JSON array)" in prompt:
        assert "기본값 테스트" in prompt
    else:
        assert "밝게" in prompt
        target.write_text(
            'import json\n\ndef restore(path):\n'
            '    return json.loads(path.read_text(encoding="utf8"))["theme"] if path.exists() else "light"\n',
            encoding="utf8",
        )
    return "설정 복원을 확인했습니다.\n\n## 변경 근거\n\n- 저장된 `theme` 값을 확인했습니다."
