import os
from pathlib import Path
import subprocess
import sys
import time

import pytest


@pytest.mark.skipif(os.name != "nt", reason="native Windows console inheritance")
def test_hidden_pytest_console_is_inherited_and_output_and_status_are_forwarded(tmp_path):
    runner = Path(__file__).resolve().parents[1] / "scripts" / "quiet_pytest.py"
    sample = tmp_path / "test_console.py"
    sample.write_text('''
import ctypes
import subprocess
import sys

def test_visibility():
    handle = ctypes.windll.kernel32.GetConsoleWindow()
    assert not ctypes.windll.user32.IsWindowVisible(handle)
    probe = "import ctypes; print(ctypes.windll.user32.IsWindowVisible(ctypes.windll.kernel32.GetConsoleWindow()))"
    assert subprocess.check_output([sys.executable, "-c", probe]).strip() == b"0"
    print("fixture child hidden", flush=True)

def test_exit_status():
    assert False, "expected fixture failure"
''', encoding="utf-8")
    result = subprocess.run([sys.executable, runner, "-qs", sample], cwd=tmp_path,
                            stdin=subprocess.DEVNULL, capture_output=True, text=True,
                            creationflags=subprocess.CREATE_NO_WINDOW, timeout=60)
    assert result.returncode == 1 and "1 failed, 1 passed" in result.stdout, result.stdout
    assert "fixture child hidden" in result.stdout and "expected fixture failure" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="native Windows process ownership")
@pytest.mark.parametrize("reason", ["completed", "broken_pipe"])
def test_hidden_runner_finishes_when_pytest_child_keeps_output_handle(tmp_path, reason):
    import socket
    runner = Path(__file__).resolve().parents[1] / "scripts" / "quiet_pytest.py"
    # A synthetic pytest module exits with a known result while its child
    # still owns the wrapper's output handle and a listening socket.
    (tmp_path / "leaf.py").write_text("""
import socket, time
from pathlib import Path
s = socket.socket()
s.bind(('127.0.0.1', 0))
s.listen()
Path('port').write_text(str(s.getsockname()[1]))
deadline = time.monotonic() + 20
while not Path('release').exists() and time.monotonic() < deadline:
    time.sleep(.03)
""", encoding="utf-8")
    (tmp_path / "pytest.py").write_text("""
import subprocess, sys, time
from pathlib import Path
subprocess.Popen([sys.executable, 'leaf.py'], stdout=sys.stdout, stderr=sys.stderr,
                 stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
deadline = time.monotonic() + 8
while not Path('port').exists() and time.monotonic() < deadline:
    time.sleep(.03)
print('synthetic pytest completed', flush=True)
Path('done').touch()
if 'broken_pipe' in sys.argv:
    while not Path('release').exists() and time.monotonic() < deadline:
        print('test output', flush=True)
        time.sleep(.03)
sys.exit(7)
""", encoding="utf-8")
    with subprocess.Popen([sys.executable, runner, reason], cwd=tmp_path, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          creationflags=subprocess.CREATE_NO_WINDOW) as wrapper:
        try:
            deadline = time.monotonic() + 12
            while not (tmp_path / "done").exists() and time.monotonic() < deadline and wrapper.poll() is None:
                time.sleep(.03)
            assert (tmp_path / "done").exists()
            if reason == "completed":
                assert wrapper.wait(timeout=3) == 7
                assert b"synthetic pytest completed" in wrapper.stdout.read()
            else:
                wrapper.stdout.close()
                assert wrapper.wait(timeout=3) != 0
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", int((tmp_path / "port").read_text())))
        finally:
            (tmp_path / "release").touch()
