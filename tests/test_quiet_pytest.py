import os
from pathlib import Path
import subprocess
import sys

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
