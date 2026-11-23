"""Run pytest with one hidden, inheritable Windows console for test-owned CLI tools."""
from contextlib import closing
import os
import subprocess
import sys
import threading


def main(args):
    command = [sys.executable, "-m", "pytest", *args]
    if os.name != "nt":
        return subprocess.call(command)

    # A console-less pytest process can cause each console program it starts
    # to allocate a visible window. Let ordinary fixture children inherit a
    # hidden console, including Git and Python launched by test shell scripts.
    from agent_office.studio_process import WindowsJob
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb") as stream, os.fdopen(write_fd, "wb") as writer, closing(WindowsJob()) as job:
        child = job.spawn(command, output=writer, combined=True, hidden_console=True)
        writer.close()  # Only the test subtree owns the write end now.
        errors = []
        failed = threading.Event()

        def forward():
            try:
                while block := stream.read1(65536):
                    sys.stdout.buffer.write(block)
                    sys.stdout.buffer.flush()
            except OSError as exc:
                errors.append(exc)
                failed.set()

        relay = threading.Thread(target=forward, daemon=True)
        try:
            child.stdin.close()
            relay.start()
            while (code := child.poll()) is None and not failed.wait(0.1):
                pass
        finally:
            # A descendant may own a copy of stdout after pytest exits.
            # Reap it before waiting for output EOF, also on interruption.
            job.drain(child)
        relay.join()
        if errors:
            raise errors[0]
        return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
