import copy
import json
from pathlib import Path
import subprocess

import pytest

from agent_office.adapters import AgentResult
from agent_office.meeting import conduct_meeting, validate_plan, validate_plan_scope


def git(repo, *args):
    return subprocess.check_output(["git", *args], cwd=repo, encoding="utf-8").strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Tester")
    git(tmp_path, "config", "user.email", "test@example.org")
    for name, contents in {
        "cart/cart.py": "def quantity(): return 2\n",
        "cart/test_cart.py": "def test_quantity(): assert quantity() == 2\n",
        "cart/credentials.py": "NEVER_IN_PROMPT = 1\n",
        "other/cart.py": "OUTSIDE_SELECTION = 1\n",
    }.items():
        file = tmp_path / name
        file.parent.mkdir(exist_ok=True)
        file.write_text(contents, encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "pinned")
    (tmp_path / "cart/cart.py").write_text("UNCOMMITTED = 3\n", encoding="utf-8")
    return tmp_path


def contribution(opinion, *, issues=(), questions=None):
    return {
        "opinion": opinion,
        "issues": list(issues),
        "plan": {
            "outcome": "합계를 표시한다",
            "scope": ["장바구니 화면"],
            "tasks": [
                {"id": "T1", "owner": "developer", "description": "수량의 합을 계산", "depends_on": [],
                 "folders": ["cart"]},
                {"id": "T2", "owner": "reviewer", "description": "수량 합 검토", "depends_on": ["T1"]},
            ],
            "criteria": [{"criterion": "수량 2와 3의 합이 5", "verification": "합계 테스트",
                          "test_files": ["cart/test_cart.py"]}],
            "rationale": ["cart/cart.py의 수량 표현 사용"],
            "questions": questions or [],
        },
    }


class Worker:
    def __init__(self, *replies):
        self.replies = iter(replies)
        self.prompts = []

    def execute(self, prompt, *, workspace, allow_edits, strict_completion):
        assert allow_edits is False and strict_completion is True
        assert list(Path(workspace).iterdir()) == [workspace / ".git"]
        self.prompts.append(prompt)
        reply = next(self.replies)
        return reply if isinstance(reply, AgentResult) else AgentResult(0, json.dumps(reply, ensure_ascii=False))


def meet(repo, developer, reviewer, record, **opts):
    return conduct_meeting("장바구니 수량 합계를 표시해 줘", repo, "codex", "antigravity",
                           adapters={"codex": developer, "antigravity": reviewer},
                           folders=[repo / "cart"], record=lambda *items: record.append(items), **opts)


def test_two_distinct_roles_receive_committed_scoped_source_and_test(repo):
    developer = Worker(contribution("기존 수량 함수를 활용", issues=["반올림 규칙 확인"]))
    questions = [{"question": "합계를 어디에 표시할까요?", "options": ["헤더", "결제 버튼 옆"]}]
    final = contribution("표시 위치만 사용자 결정이 필요", questions=questions)
    reviewer = Worker(final)
    record = []
    plan = meet(repo, developer, reviewer, record)
    for prompt in (developer.prompts[0], reviewer.prompts[0]):
        assert "def quantity(): return 2" in prompt and "def test_quantity():" in prompt
        assert all(word not in prompt for word in ("UNCOMMITTED", "NEVER_IN_PROMPT", "OUTSIDE_SELECTION"))
    assert "반올림 규칙 확인" in reviewer.prompts[0]
    assert plan == {**final["plan"], "evidence": {"revision": git(repo, "rev-parse", "HEAD"),
                                                "paths": ["cart/cart.py", "cart/test_cart.py"]}}
    assert record == [("developer", "codex", "기존 수량 함수를 활용"),
                      ("reviewer", "antigravity", "표시 위치만 사용자 결정이 필요")]


@pytest.mark.parametrize("remains", [False, True])
def test_bounded_technical_discussion_keeps_all_contributions(repo, remains):
    developer = Worker(contribution("초안"), contribution("숫자형을 검토했습니다"))
    reviewer = Worker(contribution("검토", issues=["숫자형을 다시 검토"]),
                      contribution("재검토", issues=["범위 미확인"] if remains else []))
    record = []
    if remains:
        with pytest.raises(ValueError, match="추가 논의"):
            meet(repo, developer, reviewer, record)
    else:
        assert meet(repo, developer, reviewer, record)["questions"] == []
    assert "숫자형을 다시 검토" in developer.prompts[1]
    assert [row[2] for row in record] == ["초안", "검토", "숫자형을 검토했습니다", "재검토"]


@pytest.mark.parametrize("bad", [
    AgentResult(124, ""), AgentResult(0, "{}", truncated=True), AgentResult(0, "{}"),
    AgentResult(0, '{"opinion":"x","opinion":"y"}'),
    dict(contribution("검토"), command=["unexpected"]),
])
def test_invalid_review_preserves_only_prior_valid_opinion(repo, bad):
    record = []
    with pytest.raises(ValueError, match=r"reviewer \(antigravity\)"):
        meet(repo, Worker(contribution("초안")), Worker(bad), record)
    assert record == [("developer", "codex", "초안")]


def test_plan_rejects_invalid_dependency_options_and_execution_hints():
    plan = {**contribution("a")["plan"], "evidence": {"revision": "a" * 40, "paths": []}}
    assert validate_plan(plan) == plan
    for section, changes in [
        ("tasks", {"id": "T2"}), ("tasks", {"depends_on": ["T2"]}),
        ("tasks", {"owner": "operator"}), ("tasks", {"folders": ["../root"]}),
        ("criteria", {"command": ["echo", "untrusted"]}), ("criteria", {"test_files": ["run.cmd"]}),
    ]:
        invalid = copy.deepcopy(plan)
        invalid[section][0].update(changes)
        with pytest.raises(ValueError):
            validate_plan(invalid)
    for questions in [
        [{"question": "choose", "options": ["one"]}],
        [{"question": "choose", "options": ["one", "one"]}],
        [{"question": "choose", "options": ["one", "two"]}] * 2,
    ]:
        with pytest.raises(ValueError):
            validate_plan({**plan, "questions": questions})
    with pytest.raises(ValueError, match="작업 폴더 밖"):
        validate_plan_scope(plan, ["other"])


def test_same_engine_selected_for_both_roles_still_requires_two_calls(repo):
    adapter = Worker(contribution("초안"), contribution("독립 검토"))
    records = []
    conduct_meeting("합계", repo, "codex", "codex", adapters={"codex": adapter},
                    record=lambda *items: records.append(items))
    assert [row[:2] for row in records] == [("developer", "codex"), ("reviewer", "codex")]


def test_moved_project_revision_never_releases_reviewed_plan(repo):
    def record(role, *_):
        if role == "reviewer":
            git(repo, "commit", "-qam", "new revision")

    with pytest.raises(ValueError, match="기준 커밋"):
        conduct_meeting("합계", repo, "codex", "codex", record=record,
                        adapters={"codex": Worker(contribution("초안"), contribution("검토"))})
