"""Own and drain one disposable CLI fixture's complete process tree."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

if __name__ == "__main__":
    # -I excludes the operator's current directory and untrusted Python paths.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_office.adapters import AgentResult, CLIAdapter
from agent_office.studio_process import run_until_owner_closes


def _command(mode):
    return [sys.executable, "-I", str(Path(__file__).resolve()), mode]


def execute_fixture(engine, prompt, *, workspace, allow_edits, timeout=120):
    """The supervisor exits after the adapter, its CLI and all descendants are gone."""
    payload = json.dumps([engine, prompt, str(workspace), allow_edits, timeout]).encode("ascii") + b"\n"
    failure = AgentResult(1, "The fixture worker did not finish.")
    with tempfile.TemporaryFile() as result:
        with subprocess.Popen(_command("supervise"), stdin=subprocess.PIPE, stdout=result,
                              stderr=subprocess.DEVNULL, start_new_session=os.name != "nt",
                              creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0) as supervisor:
            try:
                supervisor.stdin.write(payload)
                supervisor.stdin.flush()
                exit_code = supervisor.wait()
            finally:
                # Even on KeyboardInterrupt, owner EOF requests cancellation and drain.
                supervisor.stdin.close()
            if exit_code:
                return failure
        result.seek(0)
        data = result.read(100_001)
    if len(data) > 100_000:
        return failure
    try:
        return AgentResult(**json.loads(data))
    except (ValueError, TypeError, RecursionError):
        return failure


def _supervise():
    payload = sys.stdin.buffer.readline()
    timeout = json.loads(payload)[-1]
    succeeded = run_until_owner_closes(_command("worker"), payload, timeout=timeout)
    return 0 if succeeded else 1


if __name__ == "__main__":
    if sys.argv[1:] == ["supervise"]:
        sys.exit(_supervise())
    if sys.argv[1:] != ["worker"]:
        sys.exit(2)
    engine, prompt, workspace, allow_edits, timeout = json.load(sys.stdin.buffer)
    result = CLIAdapter(engine, timeout=timeout).execute(
        prompt, workspace=Path(workspace), allow_edits=allow_edits, strict_completion=True)
    sys.stdout.buffer.write(json.dumps(asdict(result), ensure_ascii=False).encode("utf-8"))
