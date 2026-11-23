"""Run pytest with one hidden, inheritable Windows console for test-owned CLI tools."""
import os
from pathlib import Path
import subprocess
import sys


def main(args):
    command = [sys.executable, "-m", "pytest", *args]
    if os.name != "nt":
        return subprocess.call(command)

    # A console-less pytest process can cause each console program it starts
    # to allocate a visible window. Let ordinary fixture children inherit a
    # hidden console, including Git and Python launched by test shell scripts.
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    with subprocess.Popen(command, startupinfo=startup, creationflags=subprocess.CREATE_NEW_CONSOLE,
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as child:
        try:
            output = sys.stdout.buffer
            while block := child.stdout.read1(65536):
                output.write(block)
                output.flush()
            return child.wait()
        except (KeyboardInterrupt, BrokenPipeError):
            if child.poll() is None:
                taskkill = Path(os.environ["SystemRoot"]) / "System32" / "taskkill.exe"
                try:
                    subprocess.run([str(taskkill), "/PID", str(child.pid), "/T", "/F"],
                                   creationflags=subprocess.CREATE_NO_WINDOW, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                if child.poll() is None:
                    child.kill()
            raise


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
