import subprocess
import sys

from agent_office.meeting_runtime import RoomMeeting


def test_planned_file_links_results_without_narrowing_default_suite(tmp_path):
    files = {"test_feature.py": "def test_feature(): assert True\n",
             "test_regression.py": "def test_regression(): assert False\n"}
    for name, contents in files.items():
        (tmp_path / name).write_text(contents, encoding="utf-8")
    evidence = {}

    class Meeting(RoomMeeting):
        def snapshot(self):
            return {"revision": 1, "plan": {"criteria": [
                {"criterion": "feature", "test_files": ["test_feature.py"]},
                {"criterion": "operator observation"},
            ]}}

        def write(self, operation, revision, criterion, status, message):
            evidence[criterion] = (status, message)

    meeting = Meeting(None, "a" * 32, tmp_path, "task", "codex", "codex", ["."], {})
    with meeting.test_command([sys.executable, "-m", "pytest", "-q"]) as argv:
        run = subprocess.run(argv, cwd=tmp_path, capture_output=True, timeout=45)
    assert run.returncode == 1 and b"1 failed, 1 passed" in run.stdout
    assert evidence["feature"][0] == "passed" and "test_feature.py" in evidence["feature"][1]
    assert evidence["operator observation"][0] == "not_verified"
