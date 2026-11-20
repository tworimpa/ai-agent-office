"""Replace only the worker entry point in the production process supervisor for crash tests."""
import os
from pathlib import Path
import sys
import threading
import time

from agent_office import goal_recovery, studio_process, studio_runner


mode, fault, marker, database, run_id = sys.argv[1:]
if mode == "supervise":
    original = studio_process.run_child

    def run_child(action, db, identifier, payload, stop, **options):
        if action != "worker":
            return original(action, db, identifier, payload, stop, **options)
        command = [sys.executable, __file__, "worker", fault, marker, str(db), identifier]
        return studio_process.run_owned_command(command, payload, stop, **options)

    studio_process.run_child = run_child
    studio_process.supervise(Path(database), run_id, sys.stdin.buffer.readline())
else:
    if fault == "checkpoint":
        original = goal_recovery.checkpoint

        def checkpoint(db, identifier, stage, state):
            original(db, identifier, stage, state)
            if stage == "test":
                os._exit(42)

        goal_recovery.checkpoint = checkpoint
    else:
        def watch():
            while not Path(marker).exists():
                time.sleep(.02)
            os._exit(42)

        threading.Thread(target=watch, daemon=True).start()
    studio_runner.worker(Path(database), run_id)
