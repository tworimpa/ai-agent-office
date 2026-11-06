import subprocess
from agent_office import cli


def test_git_failure_returns_two_without_traceback(tmp_path, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["git"], stderr="private detail")
    monkeypatch.setattr(cli, "create_worktree", fail)
    assert cli.main(["run", "--repo", str(tmp_path), "--task", "x"]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert "private detail" not in output.err
    assert "Traceback" not in output.err
