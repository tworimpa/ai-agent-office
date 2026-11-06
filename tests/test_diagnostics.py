import json

from agent_office.diagnostics import doctor


def test_doctor_reports_missing_executable(monkeypatch, capsys):
    monkeypatch.setattr("agent_office.diagnostics.shutil.which", lambda name: None)
    assert doctor(["claude", "codex"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["ready"] is False
    assert [x["name"] for x in result["executables"]] == ["git", "claude", "codex"]


def test_doctor_with_fake_version(monkeypatch, capsys):
    from subprocess import CompletedProcess
    monkeypatch.setattr("agent_office.diagnostics.shutil.which", lambda n: f"/fake/{n}")
    monkeypatch.setattr(
        "agent_office.diagnostics.subprocess.run",
        lambda argv, **kwargs: CompletedProcess(argv, 0, stdout="tool 1.0\n", stderr=""),
    )
    assert doctor(["claude"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ready"] is True
