import os
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

import pytest

from agent_office import windows_auto_task as watch


def test_task_environment_has_no_worker_path_credentials_or_remote_daemon(monkeypatch, tmp_path):
    for key in ("GH_TOKEN", "GH_CONFIG_DIR", "DOCKER_HOST", "GIT_CONFIG_GLOBAL", "PYTHONPATH", "PATH"):
        monkeypatch.setenv(key, "worker-controlled")
    environment = watch._environment(tmp_path / "System32", tmp_path, tmp_path / "roaming", tmp_path / "local")
    assert not any("worker-controlled" in value for value in environment.values())
    assert environment["PATHEXT"] == ".EXE"
    assert environment["PATH"].split(os.pathsep) == [str(tool.parent) for tool in watch.TOOLS.values()] + [
        str(tmp_path / "System32")]
    assert environment["USERPROFILE"] == str(tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="Windows Task Scheduler COM definition")
def test_task_scheduler_preview_is_fixed_and_non_elevated():
    script = Path(__file__).resolve().parents[1] / "scripts" / "register-deployment-watch.ps1"
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-File", script, "-Preview"],
                            capture_output=True, text=True, check=True, timeout=30)
    doc = ET.fromstring(result.stdout.strip())
    ns = {"": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    def value(path):
        return doc.findtext(path, namespaces=ns)
    assert value("Triggers/TimeTrigger/Repetition/Interval") == "PT5M"
    assert value("Triggers/TimeTrigger/Repetition/Duration") is None
    assert value("Principals/Principal/LogonType") == "InteractiveToken"
    assert value("Principals/Principal/RunLevel") == "LeastPrivilege"
    assert value("Settings/MultipleInstancesPolicy") == "IgnoreNew"
    assert value("Actions/Exec/Command") == str(watch.CONTROLLER / "Scripts/python.exe")
    assert value("Actions/Exec/Arguments") == "-I -m agent_office.windows_auto_task"
    assert value("Actions/Exec/WorkingDirectory") == str(watch.CONTROLLER)


@pytest.mark.skipif(os.name != "nt", reason="Windows scheduled entry point")
def test_missing_native_rights_stop_before_the_policy_runner(monkeypatch):
    def refuse():
        raise ValueError("no service rights")
    monkeypatch.setattr(watch, "_prepare", lambda: None)
    monkeypatch.setattr(watch, "_service_rights", refuse)
    with pytest.raises(ValueError, match="no service rights"):
        watch.main([])
    with pytest.raises(ValueError, match="Expected the Windows"):
        watch.main(["--target", "other"])
