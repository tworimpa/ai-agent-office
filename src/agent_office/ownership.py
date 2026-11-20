"""Local OS ownership locks; recovery never executes or kills workers."""
from contextlib import ExitStack
import errno
import os
from pathlib import Path
from .models import ACTIVE, OfficeError, validate_id, validate_db_file


class RunBusyError(OfficeError):
    def __init__(self):
        super().__init__("RUN_BUSY")


class RunGuard:
    def __init__(self, db_path: Path, run_id: str):
        validate_id(run_id)
        self.db_path = Path(db_path).expanduser().resolve()
        self.path = Path(str(self.db_path) + ".locks") / (run_id + ".lock")
        self.fd = None

    @classmethod
    def studio(cls, db_path: Path):
        """The Studio server owns a distinct lock next to per-run locks."""
        guard = cls(db_path, "0" * 32)
        guard.path = guard.path.with_name("studio-owner.lock")
        return guard

    @classmethod
    def desktop(cls, db_path: Path):
        """Serialize desktop receipt publication and teardown around the Studio owner."""
        guard = cls(db_path, "0" * 32)
        guard.path = guard.path.with_name("desktop-owner.lock")
        return guard

    @classmethod
    def worker(cls, db_path: Path, run_id: str):
        guard = cls(db_path, run_id)
        guard.path = guard.path.with_name(run_id + ".worker.lock")
        return guard

    def __enter__(self):
        validate_db_file(self.db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        os.set_inheritable(self.fd, False)
        try:
            if os.name == "nt":
                import msvcrt
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self.fd)
            self.fd = None
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise RunBusyError() from None
            raise
        return self

    def is_held(self):
        """Probe an existing owner lock. Concurrent readers never block one another."""
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return False
        try:
            if os.name == "nt":
                import msvcrt
                import pywintypes
                import win32con
                import win32file
                import winerror

                handle = msvcrt.get_osfhandle(fd)
                offset = pywintypes.OVERLAPPED()
                try:
                    win32file.LockFileEx(handle, win32con.LOCKFILE_FAIL_IMMEDIATELY, 1, 0, offset)
                except pywintypes.error as exc:
                    if exc.winerror == winerror.ERROR_LOCK_VIOLATION:
                        return True
                    raise
                win32file.UnlockFileEx(handle, 1, 0, offset)
            else:
                import fcntl
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
            return False
        finally:
            os.close(fd)

    def __exit__(self, *args):
        if self.fd is not None:
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(self.fd, 0, os.SEEK_SET)
                    msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None


def recover_run(store, run_id: str, *, host_name: str, studio_owner=False):
    run = store.get_run(run_id)
    if run.host_name != host_name:
        raise OfficeError("HOST_MISMATCH")
    if run.state not in ACTIVE:
        return run
    with ExitStack() as locks:
        if not studio_owner:
            db = store.connection
            studio = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='studio_runs'").fetchone()
            if studio and db.execute("SELECT 1 FROM studio_runs WHERE id=?", (run_id,)).fetchone():
                # A live Studio owns the launch interval before its supervisor takes the run lock.
                locks.enter_context(RunGuard.studio(store.path))
        locks.enter_context(RunGuard(store.path, run_id))
        locks.enter_context(RunGuard.worker(store.path, run_id))
        locks.enter_context(store._write())
        run = store.get_run(run_id)
        if run.host_name != host_name:
            raise OfficeError("HOST_MISMATCH")
        if run.state in ACTIVE:
            from .goal_recovery import mark_owner_gap
            mark_owner_gap(store.connection,run_id)
            store._record(run_id, operation_key=f"{run_id}:recovery:{run.next_seq}", event_type="RECOVERY_REQUIRED",
                          state="RECOVERY_REQUIRED", outcome="INTERRUPTED", payload={"phase": "run"})
        return store.get_run(run_id)
