"""Dedicated check-process owner with bounded output and descendant draining."""
import json
import os
import subprocess
import sys
import threading
import time

from .process_window import BACKGROUND_FLAGS
from .worker_environment import coding_environment
from .studio_process import LinuxChildren, WindowsJob


def execute(command, cwd, env, timeout):
    with coding_environment(env) as restricted:
        return _execute(command, cwd, restricted, timeout)


def _execute(command, cwd, env, timeout):
    supervisor = [sys.executable, "-I", "-m", "agent_office.goal_process"]
    payload = json.dumps([command, str(cwd), env, timeout]).encode("utf-8") + b"\n"
    if len(payload) > 256 * 1024:
        raise ValueError("검증 명령이 크기 제한을 초과했습니다.")
    with subprocess.Popen(supervisor, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                          creationflags=BACKGROUND_FLAGS) as proc:
        try:
            proc.stdin.write(payload)
            proc.stdin.flush()
            # Keep stdin open while waiting: its EOF is the ownership lease.
            packet = proc.stdout.readline(65536)
        finally:
            proc.stdin.close()
        if proc.wait(timeout=20) or not packet:
            raise OSError("검증 프로세스와 하위 프로세스의 종료를 확인할 수 없습니다.")
    code, output, timed_out = json.loads(packet)
    if timed_out:
        raise subprocess.TimeoutExpired(command, timeout, output=output)
    return code, output


def supervise(command, cwd, env, timeout):
    closed = threading.Event()
    def watch():
        try:
            os.read(sys.stdin.fileno(), 1)
        finally:
            closed.set()
    threading.Thread(target=watch, daemon=True).start()
    tree = WindowsJob() if os.name == "nt" else LinuxChildren()
    reader, writer = os.pipe()
    tail = bytearray()
    def capture(stream):
        with stream:
            while chunk := stream.read(8192):
                tail.extend(chunk)
                del tail[:-6000]
    with os.fdopen(reader, "rb", buffering=0) as stream, os.fdopen(writer, "wb") as output:
        try:
            process = tree.spawn(command, output, cwd=cwd, env=env, combined=True)
            output.close()
            collector = threading.Thread(target=capture, args=(stream,), daemon=True)
            collector.start()
            try:
                process.stdin.close()
                deadline = time.monotonic() + timeout
                code = process.poll()
                while code is None and not closed.is_set() and time.monotonic() < deadline:
                    closed.wait(min(0.05, max(0, deadline-time.monotonic())))
                    code = process.poll()
            finally:
                tree.drain(process)
            collector.join(10)
            if collector.is_alive():
                raise RuntimeError("검증 출력 스트림이 종료되지 않았습니다.")
            return code, tail.decode("utf-8", "replace"), code is None
        finally:
            tree.close()


if __name__ == "__main__":
    request = sys.stdin.buffer.readline(256 * 1024 + 1)
    if not request.endswith(b"\n"):
        raise ValueError("Incomplete verification request")
    print(json.dumps(supervise(*json.loads(request))), flush=True)
