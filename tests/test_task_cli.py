import json
import os
from pathlib import Path
import shutil
import subprocess
import pytest
from test_store import seeded, TASK, RUN
from agent_office import cli


def test_queries_are_readonly_and_cursor_is_stable(tmp_path, monkeypatch, capsys):
    path = tmp_path / "db"
    with seeded(path) as store:
        store.record_transition(RUN, operation_key="workspace", event_type="WORKSPACE_READY")
    before = path.read_bytes()
    def forbidden(*args, **kwargs):
        raise AssertionError("Query started external execution")
    monkeypatch.setattr(cli, "CLIAdapter", forbidden)
    monkeypatch.setattr(cli, "create_worktree", forbidden)
    for args in (["tasks", "list"], ["tasks", "show", TASK], ["events", TASK, "--limit", "1"]):
        assert cli.main(["--db", str(path), *args]) == 0
        payload = json.loads(capsys.readouterr().out)
        if args[0] == "events":
            assert payload["next_after_seq"] == 1 and payload["has_more"] is True
        assert path.read_bytes() == before
    assert cli.main(["--db", str(path), "events", TASK, "--after-seq", "2"]) == 0
    assert json.loads(capsys.readouterr().out)["next_after_seq"] == 2
    assert path.read_bytes() == before


@pytest.mark.parametrize("suffix,code", [(["tasks", "list"], "DB_NOT_FOUND"),
                                        (["tasks", "list", "--limit", "0"], "INVALID_ARGUMENT"),
                                        (["tasks", "show", "bad"], "INVALID_ARGUMENT"),
                                        (["events", TASK, "--limit", "501"], "INVALID_ARGUMENT")])
def test_query_errors_do_not_create_database(tmp_path, capsys, suffix, code):
    path = tmp_path / "missing" / "db"
    assert cli.main(["--db", str(path), *suffix]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert json.loads(output.err)["error"]["code"] == code
    assert not path.parent.exists()


def test_recover_cli_only_marks_state(tmp_path, capsys, monkeypatch):
    import platform
    path = tmp_path / "db"
    with seeded(path) as store:
        store.connection.execute("UPDATE runs SET host_name=?", (platform.node(),))
    assert cli.main(["--db", str(path), "recover", "--run-id", RUN]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "RECOVERY_REQUIRED"


def test_doctor_never_creates_database(tmp_path, capsys):
    path = tmp_path / "missing" / "db"
    assert cli.main(["--db", str(path), "doctor", "--require"]) == 0
    capsys.readouterr()
    assert not path.parent.exists()


def test_default_paths_and_explicit_candidate(tmp_path, monkeypatch):
    from agent_office import runtime_paths
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "windows"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "linux"))
    monkeypatch.setattr(runtime_paths.sys, "platform", "win32")
    assert runtime_paths.default_db_path() == tmp_path / "windows/AI-Agent-Office/stable/tasks.sqlite3"
    monkeypatch.setattr(runtime_paths.sys, "platform", "linux")
    assert runtime_paths.default_db_path() == tmp_path / "linux/ai-agent-office/stable/tasks.sqlite3"
    assert runtime_paths.resolve_db_path(tmp_path / "candidate") == (tmp_path / "candidate").resolve()


@pytest.mark.skipif(os.name != "nt", reason="Windows interpreter resolver")
def test_windows_python_fallback_without_py(tmp_path):
    powershell = shutil.which("powershell.exe")
    fake = tmp_path / "python.cmd"
    fake.write_text("@exit /b 0\n")
    script = Path(__file__).resolve().parents[1] / "scripts" / "python-command.ps1"
    env = dict(os.environ, PATH=str(tmp_path))
    result = subprocess.run([powershell, "-NoProfile", "-File", str(script)], env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(fake)
