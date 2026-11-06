import sys
from pathlib import Path
from agent_office.ownership import RunGuard

with RunGuard(Path(sys.argv[1]), sys.argv[2]):
    print("READY", flush=True)
    sys.stdin.readline()
