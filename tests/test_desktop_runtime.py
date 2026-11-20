from contextlib import contextmanager
import json
import subprocess
import sys
import threading
import time
from urllib.request import urlopen

from agent_office import desktop_runtime

def wait_for_receipt(directory, process):
    path = directory / "runtime.json"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        if process.poll() is not None:
            raise AssertionError(process.stderr.read().decode(errors="replace"))
        time.sleep(0.05)
    raise AssertionError("Runtime did not publish its identity")


@contextmanager
def runtime(directory, *extra):
    with subprocess.Popen(
        [sys.executable, "-m", "agent_office.desktop_runtime", "--data-dir", str(directory), *extra],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    ) as process:
        try:
            yield process, wait_for_receipt(directory, process)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def request(receipt, route):
    with urlopen(f"http://127.0.0.1:{receipt['port']}{route}", timeout=4) as response:
        return json.load(response)


def test_runtime_uses_studio_and_honors_only_its_own_stop(tmp_path):
    data = tmp_path / "데스크톱 data"
    journal = "b" * 32
    with runtime(data, "--remote", "build-box", "8767", journal) as (process, first):
        assert request(first, "/api/desktop-runtime") == first
        assert first["remotes"] == [{"alias": "build-box", "port": 8767, "journalId": journal}]
        assert {"id": journal, "name": "build-box"} in request(first, "/api/hosts")["hosts"]
        assert request(first, "/api/studio-identity") == {"journal_id": first["journal_id"]}
        assert request(first, "/api/workspaces") == {"workspaces": []}
        (data / f"stop-{'f' * 32}").touch()
        assert request(first, "/api/desktop-runtime") == first
        (data / f"stop-{first['boot_id']}").touch()
        assert process.wait(timeout=10) == 0
    assert not (data / "runtime.json").exists()
    with runtime(data, "--expected-journal", first["journal_id"]) as (process, second):
        assert second["journal_id"] == first["journal_id"]
        assert second["boot_id"] != first["boot_id"]
        assert second["remotes"] == []
        assert request(second, "/api/desktop-runtime") == second
        (data / f"stop-{second['boot_id']}").touch()
        assert process.wait(timeout=10) == 0


def test_saved_desktop_refuses_missing_and_replaced_journal(tmp_path):
    expected = "a" * 32
    journal = tmp_path / "tasks.sqlite3"
    for has_other_journal in (False, True):
        if has_other_journal:
            with runtime(tmp_path) as (process, receipt):
                (tmp_path / f"stop-{receipt['boot_id']}").touch()
                assert process.wait(timeout=10) == 0
            original = journal.read_bytes()
        result = subprocess.run(
            [sys.executable, "-m", "agent_office.desktop_runtime", "--data-dir", str(tmp_path),
             "--expected-journal", expected], capture_output=True, timeout=10,
        )
        assert result.returncode != 0
        assert not (tmp_path / "runtime.json").exists()
        if has_other_journal:
            assert journal.read_bytes() == original
        else:
            assert not journal.exists()


def test_second_runtime_leaves_live_receipt_and_journal_in_place(tmp_path):
    with runtime(tmp_path) as (owner, receipt):
        child = subprocess.run(
            [sys.executable, "-m", "agent_office.desktop_runtime", "--data-dir", str(tmp_path)],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
        )
        assert child.returncode
        assert request(receipt, "/api/desktop-runtime") == receipt
        assert json.loads((tmp_path / "runtime.json").read_text(encoding="utf-8")) == receipt
        (tmp_path / f"stop-{receipt['boot_id']}").touch()
        assert owner.wait(timeout=10) == 0


def test_runtime_keeps_receipt_until_studio_close_has_finished(tmp_path, monkeypatch):
    before_close = threading.Event()
    finish_close = threading.Event()
    studio = desktop_runtime.StudioServer

    class DelayedClose(studio):
        def server_close(self):
            before_close.set()
            assert finish_close.wait(10)
            super().server_close()

    monkeypatch.setattr(desktop_runtime, "StudioServer", DelayedClose)
    server = threading.Thread(target=desktop_runtime.serve, args=(tmp_path, ()))
    server.start()
    receipt_path = tmp_path / "runtime.json"
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not receipt_path.is_file() and server.is_alive():
            time.sleep(0.05)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        (tmp_path / f"stop-{receipt['boot_id']}").touch()
        assert before_close.wait(5)
        assert json.loads(receipt_path.read_text(encoding="utf-8")) == receipt
    finally:
        finish_close.set()
        server.join(15)
    assert not server.is_alive()
    assert not receipt_path.exists()
    with runtime(tmp_path) as (process, restarted):
        assert restarted["journal_id"] == receipt["journal_id"]
        (tmp_path / f"stop-{restarted['boot_id']}").touch()
        assert process.wait(timeout=10) == 0
