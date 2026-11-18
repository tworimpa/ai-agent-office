"""Own a single coordinator and its descendants until the run has stopped."""
from contextlib import ExitStack
import ctypes
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time


class LinuxChildren:
    def __init__(self):
        # Child subreaper: orphaned grandchildren (including setsid workers) reparent
        # here. This process supervises exactly one run and starts no other children.
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            raise OSError(ctypes.get_errno(), "Unable to own run descendants")

    def spawn(self, argv):
        return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def drain(self, process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        children = Path(f"/proc/self/task/{os.getpid()}/children")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid:
                continue
            for child in children.read_text().split():
                try:
                    os.kill(int(child), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            time.sleep(0.01)
        raise RuntimeError("Run descendants did not exit")

    def close(self):
        pass


class WindowsJob:
    def __init__(self):
        import win32job

        self.api = win32job
        self.handle = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(self.handle, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(self.handle, win32job.JobObjectExtendedLimitInformation, info)

    def spawn(self, argv):
        # A Windows venv launcher can create the real interpreter before Python
        # code reads stdin. Suspend its primary thread until the job owns it.
        import msvcrt
        import win32con
        import win32process

        with ExitStack() as stack:
            reader, writer = os.pipe()
            stack.callback(os.close, reader)
            stdin = os.fdopen(writer, "wb")
            try:
                output = stack.enter_context(open(os.devnull, "wb"))
                os.set_inheritable(reader, True)
                os.set_inheritable(output.fileno(), True)
                startup = win32process.STARTUPINFO()
                startup.dwFlags = subprocess.STARTF_USESTDHANDLES
                startup.hStdInput = msvcrt.get_osfhandle(reader)
                startup.hStdOutput = startup.hStdError = msvcrt.get_osfhandle(output.fileno())
                handle, thread, _, _ = win32process.CreateProcess(
                    argv[0], subprocess.list2cmdline(argv), None, None, True,
                    win32con.CREATE_SUSPENDED | subprocess.CREATE_NO_WINDOW, None, None, startup,
                )
                try:
                    self.api.AssignProcessToJobObject(self.handle, handle)
                    win32process.ResumeThread(thread)
                except BaseException:
                    win32process.TerminateProcess(handle, 1)
                    handle.Close()
                    raise
                finally:
                    thread.Close()
                return WindowsChild(handle, stdin)
            except BaseException:
                stdin.close()
                raise

    def drain(self, process):
        self.api.TerminateJobObject(self.handle, 1)
        process.wait(timeout=10)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            info = self.api.QueryInformationJobObject(self.handle, self.api.JobObjectBasicAccountingInformation)
            if info["ActiveProcesses"] == 0:
                return
            time.sleep(0.01)
        raise RuntimeError("Run descendants did not exit")

    def close(self):
        self.handle.Close()


class WindowsChild:
    def __init__(self, handle, stdin):
        self.handle = handle
        self.stdin = stdin

    def poll(self):
        import win32event
        import win32process

        if win32event.WaitForSingleObject(self.handle, 0) == win32event.WAIT_TIMEOUT:
            return None
        return win32process.GetExitCodeProcess(self.handle)

    def wait(self, timeout):
        import win32event

        if win32event.WaitForSingleObject(self.handle, timeout * 1000) == win32event.WAIT_TIMEOUT:
            raise TimeoutError("Coordinator did not exit")


def run_child(mode, db_path, run_id, payload, stop, *, timeout=None):
    tree = WindowsJob() if os.name == "nt" else LinuxChildren()
    try:
        process = tree.spawn([sys.executable, "-m", "agent_office.studio_runner", mode, str(db_path), run_id])
        try:
            # The child reads stdin before starting any Git or vendor command.
            if not stop.is_set():
                process.stdin.write(payload)
            process.stdin.close()
            deadline = None if timeout is None else time.monotonic() + timeout
            while process.poll() is None:
                if stop.wait(0.03) or (deadline is not None and time.monotonic() >= deadline):
                    return False
            return process.poll() == 0
        finally:
            try:
                if not process.stdin.closed:
                    process.stdin.close()
            finally:
                tree.drain(process)
    finally:
        tree.close()


def supervise(db_path, run_id, payload):
    """EOF signals owner loss or an explicitly persisted stop request."""
    from .models import ACTIVE
    from .ownership import RunGuard
    from .store import TaskStore
    from .studio_runner import finish_drained

    with RunGuard(db_path, run_id), TaskStore(db_path, existing=True) as store:
        if store.get_run(run_id).state not in ACTIVE:
            return
        disconnected = threading.Event()

        def watch_owner():
            try:
                os.read(sys.stdin.fileno(), 1)
            finally:
                disconnected.set()

        threading.Thread(target=watch_owner, daemon=True).start()
        stopping = store.connection.execute(
            "SELECT stop_requested FROM studio_runs WHERE id=?", (run_id,)
        ).fetchone()[0]
        success = False
        if not stopping:
            # Two complete develop/test/review attempts plus coordinator setup.
            budget = 2 * (900 + 300 + 900) + 60
            success = run_child("worker", db_path, run_id, payload, disconnected, timeout=budget)
            if not success and not disconnected.is_set():
                store.connection.execute(
                    "UPDATE studio_runs SET diagnostic=? WHERE id=? AND diagnostic=''",
                    ("실행 제한 시간을 넘겼거나 작업 프로세스가 종료되었습니다.", run_id),
                )
        # With developers/tests/reviewers gone, capture the stable worktree. Git
        # gets its own containment and deadline in case a configured helper hangs.
        preview_done = run_child("preview", db_path, run_id, b"preview\n", threading.Event(), timeout=5)
        finish_drained(store, run_id, worker_succeeded=success and not disconnected.is_set(),
                       preview_finished=preview_done)


if __name__ == "__main__":
    # Read the request before starting the owner-EOF watchdog.
    supervise(Path(sys.argv[1]), sys.argv[2], sys.stdin.buffer.readline())
