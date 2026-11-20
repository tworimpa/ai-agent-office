from pathlib import Path
import os
import shlex
import sys

from agent_office.adapters import CLIAdapter


def test_completed_meeting_does_not_wait_for_descendant_stdio_eof(tmp_path, monkeypatch):
    script = tmp_path / "fixture.py"
    release = tmp_path / "release"
    held = tmp_path / "expired"
    script.write_text(
        "import json, subprocess, sys\n"
        "from pathlib import Path\n"
        "assert sys.stdin.read() == 'meeting input'\n"
        f"code = \"import time; from pathlib import Path; end=time.monotonic()+8\\n"
        f"while not Path({str(release.as_posix())!r}).exists() and time.monotonic()<end: time.sleep(.03)\\n"
        f"if time.monotonic()>=end: Path({str(held.as_posix())!r}).touch()\"\n"
        "subprocess.Popen([sys.executable, '-c', code], stdin=subprocess.DEVNULL, "
        "stdout=sys.stdout, stderr=sys.stderr)\n"
        "print(json.dumps({'type':'turn.started'}))\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'done'}}))\n"
        "print(json.dumps({'type':'turn.completed'}))\n", encoding="utf-8")
    wrapper = tmp_path / ("codex.cmd" if os.name == "nt" else "codex")
    wrapper.write_text(f'@"{sys.executable}" "{script}" %*\n' if os.name == "nt" else
                       f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n')
    wrapper.chmod(0o700)
    monkeypatch.setattr("agent_office.adapters.resolve_executable", lambda name: str(wrapper))
    try:
        result = CLIAdapter("codex", timeout=4).execute("meeting input", workspace=tmp_path, allow_edits=False,
                                                        strict_completion=True, prompt_only=True)
        assert (result.returncode, result.message) == (0, "done")
        assert not held.exists()
    finally:
        release.touch()
