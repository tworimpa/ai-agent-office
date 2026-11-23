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

from agent_office.process_window import BACKGROUND_FLAGS
from agent_office.adapters import AgentResult, CLIAdapter
from agent_office.studio_process import run_until_owner_closes


def _command(mode):
    return [sys.executable, "-I", str(Path(__file__).resolve()), mode]


def execute_fixture(engine, prompt, *, workspace, allow_edits, timeout=120, db_path=None):
    return _execute_fixture(engine, prompt, workspace, allow_edits, timeout, db_path, None)


def execute_verification(engine, prompt, *, workspace, allow_edits, timeout=120, expected_version=None):
    """Only verify-engine may exercise a candidate contract before its receipt exists."""
    return _execute_fixture(engine, prompt, workspace, allow_edits, timeout, None, expected_version)


def _execute_fixture(engine, prompt, workspace, allow_edits, timeout, db_path, expected_version):
    """The supervisor exits after the adapter, its CLI and all descendants are gone."""
    journal = str(Path(db_path).resolve()) if db_path is not None else None
    payload = json.dumps([engine, prompt, str(workspace), allow_edits, journal, expected_version, timeout]).encode("ascii") + b"\n"
    failure = AgentResult(1, "The fixture worker did not finish.")
    with tempfile.TemporaryFile() as result:
        with subprocess.Popen(_command("supervise"), stdin=subprocess.PIPE, stdout=result,
                              stderr=subprocess.DEVNULL, start_new_session=os.name != "nt",
                              creationflags=BACKGROUND_FLAGS) as supervisor:
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
    engine, prompt, workspace, allow_edits, journal, expected_version, timeout = json.load(sys.stdin.buffer)
    adapter = CLIAdapter(engine, timeout=timeout)
    adapter.db_path = Path(journal) if journal else None
    adapter.fixture_version = expected_version
    result = adapter.execute(
        prompt, workspace=Path(workspace), allow_edits=allow_edits, strict_completion=True)
    sys.stdout.buffer.write(json.dumps(asdict(result), ensure_ascii=False).encode("utf-8"))
