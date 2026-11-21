import platform
import sqlite3
import subprocess
from types import SimpleNamespace

import pytest

from agent_office import goal_checks, goal_verification as goals


def test_junit_matches_nested_workspace_paths_without_rewriting_parameter_ids(tmp_path):
    report = tmp_path / "report.xml"
    report.write_text(r'<testsuite><testcase file="tests\test_settings.py" classname="suite" '
                      r'name="restore[C:\settings]"><failure/></testcase></testsuite>')
    expected = {r"tests/test_settings.py::suite::restore[C:\settings]": "failed"}
    assert goal_checks._pytest_results(report) == expected
    assert goal_checks.portable_cases({r"tests\test_settings.py::suite::restore[C:\settings]": "failed"}) == expected


def test_missing_reproducer_is_measured_before_any_implementation_change(goal):
    from agent_office.adapters import AgentResult
    from agent_office.workflow import build_graph
    from test_workflow import base_state, MockAdapter

    git = ["git", "-C", str(goal.root), "-c", "user.name=Fixture", "-c", "user.email=u@example.invalid"]
    (goal.root / "test_restore.py").write_text("from settings import value\ndef test_empty(): assert value(None) is None\n")
    subprocess.run([*git, "commit", "-am", "original suite only checks empty setting", "-q"], check=True)
    goal.revision = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()
    new_test = goal.root / "test_saved_theme.py"
    sessions = []
    def mailbox(role, engine, *, receive_directions=True):
        # A primary phase can close exactly once for a role in this attempt.
        assert (role, True) not in sessions
        sessions.append((role, receive_directions))
        return SimpleNamespace(expire=lambda: None, receives_directions=receive_directions)

    class Worker:
        def execute(self, prompt, *, workspace, allow_edits, inbox):
            assert allow_edits and workspace == goal.root
            if "add only a new pytest regression" in prompt:
                assert not inbox.receives_directions
                new_test.write_text("from settings import value\ndef test_dark(): assert value('dark') == 'dark'\n")
            else:
                assert inbox.receives_directions
                assert "New regression measured against unchanged baseline" in prompt
                (workspace / "settings.py").write_text("def value(saved): return saved\n")
            return AgentResult(0, "기존 설정값 복원")

    graph = build_graph({"claude": Worker(), "codex": MockAdapter([AgentResult(0, "VERDICT: APPROVED")])},
                        goal=goal, inbox_factory=mailbox)
    assert graph.invoke(base_state(goal.root))["status"] == "READY_FOR_HUMAN_REVIEW"
    assert sessions == [("developer", False), ("developer", True), ("reviewer", True)]
    proof = [r for r in goals.describe(goal.db, goal.id)["checks"] if r["attempt"] == 0 and r["status"] == "failed"]
    assert len(proof) == 1 and "test_dark" in proof[0]["failed_ids"][0]
    new_test.write_text("def test_dark(): assert True\n")
    assert not goal.verify({"attempts": 2}, None)[0]
    assert goals.describe(goal.db, goal.id)["best_attempt"] == 1


@pytest.fixture
def goal(tmp_path, monkeypatch):
    root = tmp_path / "fixture"
    root.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-c", "user.name=Fixture", "-c", "user.email=u@example.invalid",
                                        *args], cwd=root, text=True).strip()
    git("init")
    (root / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    (root / "settings.py").write_text("def value(saved): return None\n")
    (root / "test_restore.py").write_text("from settings import value\ndef test_saved(): assert value('dark') == 'dark'\n")
    (root / "test_defaults.py").write_text("from settings import value\ndef test_default(): assert value(None) is None\n")
    git("add", ".")
    git("commit", "-qm", "broken settings")
    policy = goals.default_policy(["test_restore.py"])
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE runs(run_id TEXT PRIMARY KEY)")
    db.execute("INSERT INTO runs VALUES('run')")
    db.execute("PRAGMA foreign_keys=ON")
    # Always run the same tiny, test-owned fixture in CI, including root-based
    # CI containers. Production's elevated-host refusal is tested separately.
    monkeypatch.setattr(goal_checks, "_unprivileged", lambda: True)
    monkeypatch.setattr(goals.goal_recovery, "timing", lambda *_: {"active_seconds": 0})
    monkeypatch.setattr(goals.goal_recovery, "invalidate_checkpoint", lambda *_: None)
    monkeypatch.setattr(goals.goal_recovery, "checkpoint", lambda *_: None)
    verification = goals.GoalVerification(SimpleNamespace(connection=db), "run", root, git("rev-parse", "HEAD"),
                                          ["."], {"goal_policy": policy, "reproduce_required": True})
    try:
        yield verification
    finally:
        db.close()


def test_measure_baseline_then_quick_full_regression_and_immutable_patch(goal):
    assert goal.baseline()[0]
    source = goal.root / "settings.py"
    source.write_text("def value(saved): return 'dark'\n")
    result = goal.verify({"attempts": 1, "developer_output": "restore saved value"}, None)
    assert result[:3] == (False, False, True)
    first_patch = goals.candidate_diff(goal.db, goal.id, 1)
    source.write_text("def value(saved): return saved\n")
    assert goal.verify({"attempts": 2}, None)[:3] == (True, False, False)
    report = goals.describe(goal.db, goal.id)
    assert report["best_attempt"] == 2
    assert report["candidates"][0]["fixed_count"] == 1
    assert [row["exit_code"] for row in report["checks"]] == [1, 1, 0, 1, 0, 0]
    assert report["checks"][3]["new_failures"] and report["checks"][3]["fixed_ids"]
    assert report["checks"][5]["previous_attempt"] == 1 and report["checks"][5]["fixed_since_previous_count"] == 1
    assert all(row["host"] == platform.node() and row["elapsed_seconds"] > 0 for row in report["checks"])
    assert "'dark'" in first_patch and goals.candidate_diff(goal.db, goal.id, 1) == first_patch
    assert "return saved" in goals.candidate_diff(goal.db, goal.id, 2)
    assert goal.review(2, "review", "approved", "independent verdict")
    record = goals.describe(goal.db, goal.id)["candidates"][1]
    assert record["review_status"] == "approved" and record["review_output"] == "independent verdict"
    with pytest.raises(sqlite3.IntegrityError):
        goal.db.execute("UPDATE studio_goal_candidates SET patch=''")


def test_deleted_or_skipped_tests_cannot_rank_as_progress(goal):
    assert goal.baseline()[0]
    (goal.root / "test_restore.py").write_text("import pytest\n@pytest.mark.skip\ndef test_saved(): pass\n")
    assert goal.verify({"attempts": 1}, None)[:3] == (False, False, True)
    (goal.root / "test_restore.py").write_text("def test_unrelated(): pass\n")
    assert goal.verify({"attempts": 2}, None)[0] is False
    report = goals.describe(goal.db, goal.id)
    assert report["best_attempt"] is None
    assert report["checks"][2]["newly_skipped_count"] == 1
    assert report["checks"][3]["removed_count"] == 1
    assert all(not row["eligible"] for row in report["candidates"])
    assert all(row["stage"] == "quick" for row in report["checks"][2:])


def test_identical_fix_stops_with_best_candidate(goal):
    assert goal.baseline()[0]
    (goal.root / "settings.py").write_text("def value(saved): return 'dark'\n")
    assert goal.verify({"attempts": 1}, None)[2]
    assert not goal.verify({"attempts": 2}, None)[2]
    assert goals.describe(goal.db, goal.id)["best_attempt"] == 1


def test_default_quick_uses_actual_baseline_failure_even_on_a_fresh_worktree(goal):
    goal.policy = goals.default_policy()
    assert goal.baseline()[0]
    (goal.root / "settings.py").write_text("def value(saved): return 'dark'\n")
    assert goal.verify({"attempts": 1}, None)[2]
    checks = goals.describe(goal.db, goal.id)["checks"]
    assert checks[0]["recipe_argv"][-1] == checks[2]["recipe_argv"][-1] == "test_restore.py"
    assert checks[2]["status"] == "passed" and checks[3]["status"] == "failed"


def test_modified_original_assertion_with_unchanged_test_id_blocks_success(goal):
    assert goal.baseline()[0]
    (goal.root / "test_restore.py").write_text("from settings import value\ndef test_saved(): assert True\n")
    assert not goal.verify({"attempts": 1}, None)[0]
    report = goals.describe(goal.db, goal.id)
    assert report["best_attempt"] is None and report["candidates"][0]["scope_changes"]


def test_review_rejection_cannot_be_retried_with_the_same_passing_patch(goal):
    assert goal.baseline()[0]
    (goal.root / "settings.py").write_text("def value(saved): return saved\n")
    assert goal.verify({"attempts": 1}, None)[0]
    assert goal.review(1, "review", "changes_requested", "requested additional behavior")
    assert goal.verify({"attempts": 2}, None)[:3] == (False, False, False)
    assert goals.describe(goal.db, goal.id)["candidates"][1]["status"] == "changes_requested"


def test_wrong_failure_and_out_of_scope_changes_do_not_start_candidate_tests(goal):
    (goal.root / "settings.py").write_text("def value(saved): return 'dark'\n")
    subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=u@example.invalid",
                    "commit", "-qam", "unrelated default regression"], cwd=goal.root, check=True)
    goal.revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=goal.root, text=True).strip()
    assert not goal.baseline()[0]
    (goal.root / "settings.py").write_text("def value(saved): return saved\n")
    goal.folders = ["elsewhere"]
    assert goal.verify({"attempts": 1}, None)[:3] == (False, False, False)
    report = goals.describe(goal.db, goal.id)
    assert report["candidates"][0]["status"] == "unverified" and all(r["attempt"] == 0 for r in report["checks"])


def test_unavailable_or_elevated_hosts_never_spawn(tmp_path, monkeypatch):
    def unexpected(*_args, **_kwargs):
        pytest.fail("verification subprocess launched")
    monkeypatch.setattr(subprocess, "run", unexpected)
    check = goals.default_policy()["checks"][0]
    monkeypatch.setattr(goal_checks, "_unprivileged", lambda: False)
    for host, allowed in (("local", False), ("unavailable-host", True)):
        monkeypatch.setattr(goal_checks, "_unprivileged", lambda: allowed)
        receipt, cases = goal_checks.run_check(tmp_path, dict(check, host=host), timeout=2)
        assert receipt["status"] == "unverified" and receipt["exit_code"] is None and not cases


@pytest.mark.parametrize("argv", [["cmd", "/c", "echo"], ["python", "-c", "pass"],
                                  ["pytest", "-q", "-p", "plugin"], ["pytest", "-q", "../test_unit.py"],
                                  ["node", "desktop/verify-install.mjs", "/tmp/custom"], ["npm", "run", "postinstall"]])
def test_policy_validates_recipes_before_launch(argv):
    policy = goals.default_policy()
    policy["checks"][0]["argv"] = argv
    with pytest.raises(ValueError):
        goals.validate_policy(policy)
