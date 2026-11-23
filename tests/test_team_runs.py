"""Exercise real Git worktrees and owned subprocesses with synthetic CLI workers."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import sqlite3
from pathlib import Path
import subprocess
import sys
import threading
import uuid

import pytest

from agent_office.models import OfficeError, OwnerInfo, TaskSpec
from agent_office.workspace import plan_worktree
from agent_office.role_skills import RoleSkillRegistry
from agent_office.store import TaskStore
from agent_office.studio_catalog import StudioCatalog
from agent_office.studio_runner import LocalRunner
from agent_office.team_runs import aggregate
from test_studio_runner import install_fake, repo, wait_for


def git(repo, *args):
    return subprocess.check_output(["git", "-c", "user.name=studio", "-c", "user.email=studio@example.invalid",
                                    *args], cwd=repo, text=True).strip()


def test_plain_cli_and_studio_parents_cannot_share_an_id(tmp_path, repo, monkeypatch):
    install_fake(tmp_path, monkeypatch, "print('plan')\n")
    db = tmp_path / "journal.db"
    wid, cli_run, group_id = (uuid.uuid4().hex for _ in range(3))
    plan = [{"task": name, "indices": [0]} for name in ("one", "two")]
    with closing(LocalRunner(db)) as runner, TaskStore(db) as store:
        def create_cli(run_id):
            spec = TaskSpec(uuid.uuid4().hex, str(repo), "CLI", "codex", "antigravity", 1)
            store.create_run(spec, run_id, plan_worktree(repo, run_id), OwnerInfo("fixture", uuid.uuid4().hex))

        create_cli(cli_run)
        with pytest.raises(OfficeError, match="REQUEST_CONFLICT"):
            runner.start(cli_run, wid, repo, ["."], "group", False, subtasks=plan)
        assert runner.runs() == []
        parent = runner.start(group_id, wid, repo, ["."], "group", False, subtasks=plan)
        with pytest.raises(sqlite3.IntegrityError):
            create_cli(group_id)
        assert [t["id"] for t in runner.detail(group_id)["subtasks"]] == [t["id"] for t in parent["subtasks"]]


@pytest.mark.parametrize("slots", [1, 2])
def test_parallel_children_pin_base_scope_skills_and_retain_partial_results(tmp_path, repo, monkeypatch, slots):
    if slots == 1:
        monkeypatch.setenv("AGENT_OFFICE_TEAM_SLOTS", "1")
    else:
        monkeypatch.delenv("AGENT_OFFICE_TEAM_SLOTS", raising=False)
    db = tmp_path / "office.db"
    for name in ("api", "ui"):
        (repo / name).mkdir()
        (repo / name / "input").write_text("base")
    (repo / "test_feature.py").write_text("def test_feature():\n    assert 2 + 2 == 4\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "initial fixture")
    base = git(repo, "rev-parse", "HEAD")
    gate = tmp_path / "allow-review"
    install_fake(tmp_path, monkeypatch,
        "import pathlib, sys, time\n"
        "prompt = sys.stdin.read()\n"
        "assert 'Parent constraint TOKEN=keep_this_only_in_memory' in prompt\n"
        "label = prompt.split('Assigned subtask:\\n')[1].splitlines()[0]\n"
        "if 'Review the requested changes' in prompt:\n"
        "    assert 'review-skill-1' in prompt and 'dev-skill-1' not in prompt\n"
        "    print('VERDICT: APPROVED\\n' + label); sys.exit()\n"
        "assert 'dev-skill-1' in prompt and 'review-skill-1' not in prompt\n"
        f"marker = pathlib.Path({str(tmp_path)!r}) / ('called-' + label)\n"
        "assert not marker.exists(); marker.touch()\n"
        "pathlib.Path('result.txt').write_text(label)\n"
        f"while not pathlib.Path({str(gate)!r}).exists(): time.sleep(.02)\n"
        "print('worker ' + label); sys.exit(1 if label == 'third' else 0)\n")
    catalog = StudioCatalog(db)
    wid = catalog.create("project", [str(repo / "ui"), str(repo / "api")])["id"]
    rid = uuid.uuid4().hex
    registry = RoleSkillRegistry(db)
    dev = registry.create("development", "dev-skill-1")
    review = registry.create("review", "review-skill-1")
    selection = {"developer": [dev["id"]], "reviewer": [review["id"]]}
    plan = [{"task": "first", "indices": [1]}, {"task": "second", "indices": [0]},
            {"task": "third", "indices": [1, 0]}]
    args = (catalog, rid, wid, [1, 0], "Parent constraint TOKEN=keep_this_only_in_memory", True)
    options = dict(reviewer_engine="codex", role_skills=selection, subtasks=plan)
    with closing(LocalRunner(db)) as runner:
        with ThreadPoolExecutor(max_workers=3) as pool:
            receipts = list(pool.map(lambda _: runner.submit(*args, **options), range(3)))
        receipt = receipts[0]
        ids = [item["id"] for item in receipt["subtasks"]]
        assert all([item["id"] for item in r["subtasks"]] == ids for r in receipts)
        assert receipt["kind"] == "parallel" and receipt["role_skills"] == selection
        assert receipt["base_revision"] == base and receipt["indices"] == [1, 0]
        assert receipt["task"] == "Parent constraint TOKEN=[redacted]"
        assert receipt["role_skill_snapshots"] == {"developer": [dev], "reviewer": [review]}
        assert [c["indices"] for c in receipt["subtasks"]] == [[1], [0], [1, 0]]
        assert [c["task"] for c in receipt["subtasks"]] == ["first", "second", "third"]
        assert [c["parent_id"] for c in receipt["subtasks"]] == [rid] * 3
        wait_for(lambda: sum(1 for _ in tmp_path.glob("called-*")) == slots)
        assert len(runner.team_workers) == slots
        assert not (tmp_path / "called-third").exists()
        registry.create("development", "dev-skill-2")
        (repo / "new-source").touch()
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "advance source while third task is queued")
        gate.touch()
        wait_for(lambda: runner.detail(rid)["state"] == "FAILED")
        children = [runner.detail(child_id) for child_id in ids]
        assert [c["state"] for c in children] == ["READY_FOR_HUMAN_REVIEW", "READY_FOR_HUMAN_REVIEW", "FAILED"]
        assert runner.detail(rid)["outcome"] == "DEVELOPER_FAILED"
        assert [c["folders"] for c in children] == [["api"], ["ui"], ["api", "ui"]]
        assert [Path(c["workspace"], "result.txt").read_text() for c in children] == ["first", "second", "third"]
        assert not (repo / "result.txt").exists()
        for c in children:
            assert c["parent_id"] == rid and c["role_skills"] == selection and c["base_revision"] == base
            assert git(c["workspace"], "rev-parse", "HEAD") == base
            assert "result.txt" in c["changes"] and c["task"] in c["diff"]
        for c in children[:2]:
            assert "1 passed" in c["test_output"] and "VERDICT: APPROVED" in c["review_output"]
        assert children[2]["review_output"] == children[2]["test_output"] == ""
        assert [row["id"] for row in runner.runs()] == [rid]
        with pytest.raises(OfficeError, match="REQUEST_CONFLICT"):
            runner.submit(*args, reviewer_engine="codex")
        with pytest.raises(OfficeError, match="REQUEST_CONFLICT"):
            runner.submit(catalog, ids[0], *args[2:], **options)
    repo.rename(tmp_path / "moved")
    with closing(LocalRunner(db)) as reopened:
        assert reopened.submit(*args, **options)["subtasks"] == reopened.detail(rid)["subtasks"]
        assert reopened.detail(rid)["state"] == "FAILED"
    assert b"keep_this_only_in_memory" not in db.read_bytes()


@pytest.mark.parametrize("end", ["stop", "owner_loss"])
def test_no_queued_execution_after_parent_stop_or_owner_loss(tmp_path, repo, monkeypatch, end):
    db = tmp_path / "office.db"
    install_fake(tmp_path, monkeypatch,
        "import pathlib, sys, time\n"
        "task = sys.stdin.read().split('Assigned subtask:\\n')[1].splitlines()[0]\n"
        f"(pathlib.Path({str(tmp_path)!r}) / ('called-' + task)).touch()\n"
        "time.sleep(60)\n")
    rid, wid = uuid.uuid4().hex, uuid.uuid4().hex
    args = (rid, wid, repo, ["."], "Original", False)
    plan = [dict(task=str(i), indices=[0]) for i in range(4)]
    wait_two = lambda: sum(1 for _ in tmp_path.glob("called-*")) == 2
    if end == "stop":
        with closing(LocalRunner(db)) as runner:
            receipt = runner.start(*args, subtasks=plan)
            wait_for(wait_two)
            other = runner.start(uuid.uuid4().hex, *args[1:], subtasks=[
                {"task": "other-1", "indices": [0]}, {"task": "other-2", "indices": [0]}])
            with runner.lock, TaskStore(db) as store:
                runner._schedule(store)
                assert all(c["id"] in runner.pending for c in other["subtasks"])
            assert runner.stop(other["subtasks"][0]["id"])["state"] == "CANCELED"
            assert runner.stop(other["id"])["state"] == "CANCELED"
            assert runner.stop(rid)["state"] in ("STOP_REQUESTED", "CANCELED")
            wait_for(lambda: runner.detail(rid)["state"] == "CANCELED")
            assert [x["state"] for x in runner.detail(rid)["subtasks"]] == ["CANCELED"] * 4
    else:
        # A true process exit bypasses LocalRunner.close and releases the owner pipes/locks.
        receipt_file = tmp_path / "receipt.json"
        script = (
            "import json, os, pathlib, time\n"
            "from agent_office.studio_runner import LocalRunner\n"
            f"owner = LocalRunner(pathlib.Path({str(db)!r}))\n"
            f"receipt = owner.start({rid!r}, {wid!r}, pathlib.Path({str(repo)!r}), ['.'], 'Original', False, "
            f"subtasks={plan!r})\n"
            f"pathlib.Path({str(receipt_file)!r}).write_text(json.dumps(receipt))\n"
            "deadline = time.monotonic() + 30\n"
            f"while len(list(pathlib.Path({str(tmp_path)!r}).glob('called-*'))) < 2:\n"
            "    if time.monotonic() >= deadline: raise RuntimeError('workers did not start')\n"
            "    time.sleep(.02)\n"
            "os._exit(19)\n")
        assert subprocess.run([sys.executable, "-c", script], timeout=45).returncode == 19
        receipt = json.loads(receipt_file.read_text())
    expected = "RECOVERY_REQUIRED" if end == "owner_loss" else "CANCELED"
    with closing(LocalRunner(db)) as reopened:
        wait_for(lambda: reopened.detail(rid)["state"] == expected)
        replay = reopened.start(*args, subtasks=plan)
        assert replay["state"] == expected
        assert [c["id"] for c in replay["subtasks"]] == [c["id"] for c in receipt["subtasks"]]
        assert all(c["state"] == expected for c in replay["subtasks"])
        assert sorted(p.name for p in tmp_path.glob("called-*")) == ["called-0", "called-1"]
        assert all(not Path(reopened.detail(c["id"])["workspace"]).exists() for c in replay["subtasks"][2:])


@pytest.mark.parametrize(("states", "expected"), [
    (["PLAN_ONLY", "PLAN_ONLY"], ("PLAN_ONLY", "PLAN_ONLY")),
    (["READY_FOR_HUMAN_REVIEW"] * 2, ("READY_FOR_HUMAN_REVIEW", "APPROVED")),
    (["READY_FOR_HUMAN_REVIEW", "WAITING_USER"], ("WAITING_USER", "REVIEW_UNDECIDED")),
    (["READY_FOR_HUMAN_REVIEW", "PLAN_ONLY"], ("RECOVERY_REQUIRED", "INTERRUPTED")),
])
def test_group_terminal_outcomes(states, expected):
    outcomes = {"PLAN_ONLY": "PLAN_ONLY", "READY_FOR_HUMAN_REVIEW": "APPROVED", "WAITING_USER": "REVIEW_UNDECIDED"}
    assert aggregate([{"state": state, "outcome": outcomes[state]} for state in states], False) == expected


def test_invalid_child_rolls_back_entire_request(tmp_path, repo):
    (repo / "a").mkdir()
    (repo / "b").mkdir()
    for folder in ("a", "b"):
        (repo / folder / "README.md").touch()
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "folders")
    path = tmp_path / "office.db"
    with closing(LocalRunner(path)) as runner:
        rid = uuid.uuid4().hex
        args = (rid, uuid.uuid4().hex, repo, ["a", "b"], "parent", True)
        policy = {"specialist_engine": "codex", "verifier_engine": "claude",
                  "rules": [{"topic": "test_command", "path": "a/README.md"}]}
        with pytest.raises(ValueError):
            runner.start(*args, use_inbox=True, delegation=policy, subtasks=[
                {"task": "first", "indices": [0]}, {"task": "second", "indices": [1]}])
        assert runner.runs() == []
        with TaskStore(path, readonly=True) as store:
            assert store.list_tasks() == []
            assert not store.connection.execute("SELECT 1 FROM studio_teams").fetchone()


def test_dispatch_failure_requires_recovery_without_replaying(tmp_path, repo, monkeypatch):
    def interrupted(store, run_id, prompt):
        raise RuntimeError("simulated launch failure after dequeue")

    with closing(LocalRunner(tmp_path / "db")) as runner:
        monkeypatch.setattr(runner, "_launch", interrupted)
        args = (uuid.uuid4().hex, uuid.uuid4().hex, repo, ["."], "group", False)
        plan = [{"task": "one", "indices": [0]}, {"task": "two", "indices": [0]}]
        runner.start(*args, subtasks=plan)
        wait_for(lambda: runner.detail(args[0])["state"] == "RECOVERY_REQUIRED")
        assert all(row["state"] == "RECOVERY_REQUIRED" for row in runner.start(*args, subtasks=plan)["subtasks"])
        assert not runner.pending and not runner.children


def test_thread_start_failure_commits_no_receipt(tmp_path, repo, monkeypatch):
    def unavailable(thread):
        raise RuntimeError("can't start new thread")

    with closing(LocalRunner(tmp_path / "db")) as runner:
        monkeypatch.setattr(threading.Thread, "start", unavailable)
        with pytest.raises(RuntimeError, match="can't start new thread"):
            runner.start(uuid.uuid4().hex, uuid.uuid4().hex, repo, ["."], "group", False,
                         subtasks=[{"task": "one", "indices": [0]}, {"task": "two", "indices": [0]}])
        assert runner.runs() == [] and not runner.pending


def test_recover_cli_cannot_take_a_child_during_supervisor_handoff(tmp_path, repo, monkeypatch):
    db = tmp_path / "db"
    install_fake(tmp_path, monkeypatch, "print('plan')\n")
    with closing(LocalRunner(db)) as runner:
        launch = runner._launch
        attempts = []

        def recover(run_id):
            return subprocess.run([sys.executable, "-m", "agent_office.cli", "--db", str(db),
                                   "recover", "--run-id", run_id], capture_output=True, text=True, timeout=20)

        def launch_after_recovery_attempt(store, run_id, prompt):
            attempts.append(recover(run_id))
            launch(store, run_id, prompt)

        monkeypatch.setattr(runner, "_launch", launch_after_recovery_attempt)
        receipt = runner.start(uuid.uuid4().hex, uuid.uuid4().hex, repo, ["."], "group", False,
                               subtasks=[{"task": "one", "indices": [0]}, {"task": "two", "indices": [0]}])
        wait_for(lambda: runner.detail(receipt["id"])["state"] == "PLAN_ONLY")
        assert len(attempts) == 2
        assert all(result.returncode == 2 and "RUN_BUSY" in result.stderr for result in attempts)
        terminal = recover(receipt["subtasks"][0]["id"])
        assert terminal.returncode == 0 and json.loads(terminal.stdout)["state"] == "PLAN_ONLY"
