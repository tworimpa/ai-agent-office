import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import pytest
from agent_office import cli
from agent_office.adapters import CLIAdapter, AgentResult
from agent_office.ownership import RunGuard
from agent_office.store import TaskStore
from agent_office.workflow import build_graph
from test_store import seeded, TASK, RUN
from test_workflow import MockAdapter, base_state


def test_truncated_review_cannot_hide_conflicting_verdict(tmp_path, monkeypatch):
    text = "VERDICT: APPROVED\n" + "x" * 13000 + "\nVERDICT: CHANGES_REQUESTED"
    monkeypatch.setattr("agent_office.adapters.shutil.which", lambda name: sys.executable)
    with monkeypatch.context() as patch:
        patch.setattr("agent_office.adapters.subprocess.run", lambda *a, **k: subprocess.CompletedProcess([], 0, json.dumps({"result": text}), ""))
        result = CLIAdapter("claude").execute("review", workspace=tmp_path, allow_edits=False)
    graph = build_graph({"claude": MockAdapter([AgentResult(0, "done")]), "codex": MockAdapter([result])})
    assert graph.invoke(base_state(tmp_path))["status"] == "NEEDS_HUMAN_REVIEW"


@pytest.mark.parametrize("text", ['{"PASSWORD":"pasted-secret"}', 'PASSWORD="abc\\"pasted-secret"', "{'API_KEY':'pasted-secret'}"])
def test_quoted_and_escaped_secrets_are_not_saved(text, tmp_path):
    from agent_office.events import snapshot_request
    from agent_office.models import TaskSpec, OwnerInfo
    from agent_office.workspace import Worktree
    assert "pasted-secret" not in snapshot_request(text)
    db = tmp_path / "db"
    with TaskStore(db) as store:
        store.create_run(TaskSpec(TASK, "project", text, "claude", "codex", 1), RUN,
                         Worktree(tmp_path / "work", "ao/task"), OwnerInfo("host", "owner"))
    with TaskStore(db, readonly=True) as store:
        assert "pasted-secret" not in store.get_task(TASK).request_snapshot
    assert b"pasted-secret" not in db.read_bytes()


@pytest.mark.parametrize("kind", ["empty", "wrong_shape"])
def test_recover_never_initializes_unrecognized_existing_db(tmp_path, capsys, kind):
    db = tmp_path / "db"
    db.touch()
    if kind == "wrong_shape":
        with sqlite3.connect(db) as con:
            con.execute("CREATE TABLE tasks(task_id TEXT, updated_at TEXT)")
            con.execute("INSERT INTO tasks VALUES (?,?)", (TASK, "now"))
            con.execute("PRAGMA user_version=1")
    before = db.read_bytes()
    assert cli.main(["--db", str(db), "recover", "--run-id", RUN]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert json.loads(output.err)["error"]["code"] == "SCHEMA_UNSUPPORTED"
    assert db.read_bytes() == before
    if kind == "wrong_shape":
        assert cli.main(["--db", str(db), "tasks", "list"]) == 2
        assert json.loads(capsys.readouterr().err)["error"]["code"] == "SCHEMA_UNSUPPORTED"


def test_hardlink_alias_cannot_bypass_run_lock(tmp_path, capsys):
    db, alias = tmp_path / "db", tmp_path / "alias"
    with seeded(db) as store:
        store.connection.execute("UPDATE runs SET host_name=?", (platform.node(),))
        with RunGuard(db, RUN):
            os.link(db, alias)
            assert cli.main(["--db", str(alias), "recover", "--run-id", RUN]) == 2
            assert json.loads(capsys.readouterr().err)["error"]["code"] == "INVALID_ARGUMENT"
            assert store.get_run(RUN).state == "QUEUED"


def test_git_unicode_root_in_windows_legacy_encoding(tmp_path):
    repo = tmp_path / "한글 space"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "init"], cwd=repo, check=True)
    script = "from agent_office.workspace import create_worktree; from pathlib import Path; import sys; w=create_worktree(Path(sys.argv[1])); assert w.path.exists()"
    env = dict(os.environ, PYTHONUTF8="0", PYTHONIOENCODING="cp949")
    result = subprocess.run([sys.executable, "-c", script, str(repo)], env=env, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode("cp949", errors="replace")


def test_view_only_database_is_not_claimed_or_modified(tmp_path):
    db = tmp_path / "db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE VIEW unrelated AS SELECT 1")
    before = db.read_bytes()
    with pytest.raises(Exception) as error:
        TaskStore(db)
    assert error.value.code == "SCHEMA_UNSUPPORTED"
    assert before == db.read_bytes()


@pytest.mark.parametrize("args", [["events", TASK, "--after-seq", str(2**63)],
                                 ["events", TASK, "--run-id", ""],
                                 ["run", "--repo", ".", "--task", "x", "--max-attempts", str(2**63)]])
def test_invalid_boundaries_have_json_error_without_traceback(tmp_path, capsys, args):
    db = tmp_path / "db"
    seeded(db).close()
    before = db.read_bytes()
    assert cli.main(["--db", str(db), *args]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert json.loads(output.err)["error"]["code"] == "INVALID_ARGUMENT"
    assert db.read_bytes() == before


def test_empty_state_environment_uses_home_default(tmp_path, monkeypatch):
    from agent_office.runtime_paths import default_db_path
    monkeypatch.setenv("XDG_STATE_HOME", "")
    monkeypatch.setenv("LOCALAPPDATA", "")
    expected = Path.home() / ("AppData/Local/AI-Agent-Office/stable/tasks.sqlite3" if sys.platform == "win32" else ".local/state/ai-agent-office/stable/tasks.sqlite3")
    assert default_db_path() == expected
