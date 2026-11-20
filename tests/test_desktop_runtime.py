from contextlib import contextmanager
import json
import subprocess
import sys
import time
from urllib.request import urlopen


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
def runtime(directory):
    with subprocess.Popen(
        [sys.executable, "-m", "agent_office.desktop_runtime", "--data-dir", str(directory)],
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
    with runtime(data) as (process, first):
        assert request(first, "/api/desktop-runtime") == first
        assert request(first, "/api/studio-identity") == {"journal_id": first["journal_id"]}
        assert request(first, "/api/workspaces") == {"workspaces": []}
        (data / f"stop-{'f' * 32}").touch()
        assert request(first, "/api/desktop-runtime") == first
        (data / f"stop-{first['boot_id']}").touch()
        assert process.wait(timeout=10) == 0
    assert not (data / "runtime.json").exists()
    with runtime(data) as (process, second):
        assert second["journal_id"] == first["journal_id"]
        assert second["boot_id"] != first["boot_id"]
        assert request(second, "/api/desktop-runtime") == second
        (data / f"stop-{second['boot_id']}").touch()
        assert process.wait(timeout=10) == 0


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
