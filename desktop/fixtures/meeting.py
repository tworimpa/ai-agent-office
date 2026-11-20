"""Shared deterministic plan for the installed-desktop fixture engines."""
import json


def meeting_result(prompt):
    stage = prompt.splitlines()[0]
    assert stage in ("MEETING_DEVELOPER", "MEETING_REVIEWER")
    material = json.loads(prompt.splitlines()[-1])
    assert "tests/test_local.py" in material["evidence"]["excerpts"]
    if stage == "MEETING_REVIEWER":
        assert material["previous"]["opinion"]
    goal = material["request"]
    simple = "작업 파일을 설명해 주세요" in goal
    revised = "Operator direction for developer" in goal
    if revised and not simple:
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
    if revised and not simple:
        plan["outcome"] = "수정한 목표와 추가 지시 반영"
        plan["tasks"][0]["description"] = "note.txt 한 줄을 desktop verified로 변경하고 줄바꿈 유지"
        plan["rationale"].append("추가 지시의 문구를 적용한 뒤 지정 테스트를 다시 실행한다.")
    return {"opinion": stage + " 기존 코드와 테스트를 확인했습니다.", "issues": [], "plan": plan}
