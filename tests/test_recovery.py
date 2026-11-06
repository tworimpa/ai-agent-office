import os
from pathlib import Path
import subprocess
import sys
import pytest
from test_store import seeded, TASK, RUN


def test_live_owner_is_protected_and_dead_owner_recovery_is_idempotent(tmp_path):
    from agent_office.ownership import recover_run
    path = tmp_path / "db"
    with seeded(path) as store:
        helper = Path(__file__).parent / "helpers" / "fake_run_owner.py"
        owner = subprocess.Popen([sys.executable, str(helper), str(path), RUN], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            assert owner.stdout.readline().strip() == "READY"
            with pytest.raises(Exception) as error:
                recover_run(store, RUN, host_name="host")
            assert error.value.code == "RUN_BUSY"
            assert store.get_run(RUN).state == "QUEUED"
            assert len(store.list_events(TASK)) == 1
            owner.kill()
            owner.wait(timeout=5)
            assert store.get_run(RUN).state == "QUEUED"  # observation has no recovery side effect
            assert recover_run(store, RUN, host_name="host").state == "RECOVERY_REQUIRED"
            assert recover_run(store, RUN, host_name="host").state == "RECOVERY_REQUIRED"
            assert len(store.list_events(TASK)) == 2
        finally:
            if owner.poll() is None:
                owner.kill()
            owner.communicate(timeout=5)


def test_lock_file_preserved_and_not_inherited(tmp_path):
    from agent_office.ownership import RunGuard
    path = tmp_path / "db"
    child = None
    try:
        with RunGuard(path, RUN) as guard:
            assert not os.get_inheritable(guard.fd)
            inode = guard.path.stat().st_ino
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"], close_fds=False)
        with RunGuard(path, RUN) as guard:
            assert guard.path.stat().st_ino == inode
            assert child.poll() is None
    finally:
        if child:
            child.kill()
            child.wait(timeout=5)


def test_foreign_host_and_bad_ids_are_rejected(tmp_path):
    from agent_office.ownership import RunGuard, recover_run
    with seeded(tmp_path / "db") as store:
        with pytest.raises(Exception) as error:
            recover_run(store, RUN, host_name="elsewhere")
        assert error.value.code == "HOST_MISMATCH"
        assert len(store.list_events(TASK)) == 1
    with pytest.raises(ValueError):
        RunGuard(tmp_path / "db", "../../escape")
