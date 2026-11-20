from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from agent_office.goal_process import execute
from test_studio_runner import process_alive, wait_for


@pytest.mark.parametrize("stop", ["deadline", "owner_eof"])
def test_verification_owns_detached_descendants(tmp_path, stop):
    marker = tmp_path / "child.pid"
    script = ("import os,sys,subprocess,time,pathlib;"
              "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],start_new_session=os.name!='nt');"
              f"pathlib.Path({str(marker)!r}).write_text(str(p.pid));time.sleep(60)")
    command = [sys.executable, "-c", script]
    args = [command, str(tmp_path), dict(PATH=os.environ["PATH"]), 3 if stop == "deadline" else 3600]
    if os.name == "nt":
        args[2]["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    with subprocess.Popen([sys.executable, "-I", "-m", "agent_office.goal_process"],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE) as owner, ExitStack() as handles:
        owner.stdin.write(json.dumps(args).encode() + b"\n")
        owner.stdin.flush()
        wait_for(lambda: marker.is_file() and marker.read_text())
        alive = process_alive(int(marker.read_text()), handles)
        assert alive()
        if stop == "owner_eof":
            owner.stdin.close()
        assert owner.wait(timeout=20) == 0
        code, _, interrupted = json.loads(owner.stdout.read())
        assert code is None and interrupted and not alive()


def test_verification_preserves_exit_and_bounded_combined_output(tmp_path):
    code, output = execute([sys.executable, "-c",
                            "import sys; print('a'*80000,flush=True);print('end',file=sys.stderr);sys.exit(7)"],
                           tmp_path, {"PATH": os.environ["PATH"]}, 15)
    assert code == 7 and output.strip().endswith("end") and len(output) <= 6000
