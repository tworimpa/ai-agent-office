"""Test-only coordinator with deterministic external-side-effect crash windows."""
import subprocess
import sys
import time
from pathlib import Path
from agent_office import cli
from agent_office.adapters import AgentResult

db, repo, window, marker, counter, pidfile = sys.argv[1:]


def pause():
    Path(marker).write_text("ready")
    time.sleep(30)


original_create = cli.TaskStore.create_run
def create(*args, **kwargs):
    result = original_create(*args, **kwargs)
    if window == "intent":
        pause()
    return result
cli.TaskStore.create_run = create

original_workspace = cli.create_worktree
def workspace(*args, **kwargs):
    result = original_workspace(*args, **kwargs)
    if window == "workspace":
        pause()
    return result
cli.create_worktree = workspace

original_record = cli.TaskStore.record_transition
def record(*args, **kwargs):
    if window == "completed" and kwargs.get("event_type") == "STAGE_COMPLETED":
        pause()
    return original_record(*args, **kwargs)
cli.TaskStore.record_transition = record


class FakeAdapter:
    def __init__(self, *args, **kwargs):
        pass

    def execute(self, *args, **kwargs):
        script = "import os,sys,time; from pathlib import Path; Path(sys.argv[1]).open('a').write('call\\n'); Path(sys.argv[2]).write_text(str(os.getpid())); "
        if window == "started":
            script += "Path(sys.argv[3]).write_text('ready'); time.sleep(30)"
        child = subprocess.Popen([sys.executable, "-c", script, counter, pidfile, marker], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        child.wait()
        return AgentResult(0, "done")


cli.CLIAdapter = FakeAdapter
raise SystemExit(cli.main(["--db", db, "run", "--repo", repo, "--task", "fake crash test"]))
