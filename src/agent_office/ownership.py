"""Local OS ownership locks; recovery never executes or kills workers."""
import errno
import os
from pathlib import Path
from .models import ACTIVE, OfficeError, validate_id


class RunBusyError(OfficeError):
    def __init__(self):
        super().__init__("RUN_BUSY")


class RunGuard:
    def __init__(self, db_path: Path, run_id: str):
        validate_id(run_id)
        self.path = Path(str(Path(db_path).expanduser().resolve()) + ".locks") / (run_id + ".lock")
        self.fd = None

    def __enter__(self):
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


def recover_run(store, run_id: str, *, host_name: str):
    if store.get_run(run_id).host_name != host_name:
        raise OfficeError("HOST_MISMATCH")
    with RunGuard(store.path, run_id), store._write():
        run = store.get_run(run_id)
        if run.host_name != host_name:
            raise OfficeError("HOST_MISMATCH")
        if run.state in ACTIVE:
            store._record(run_id, operation_key=f"{run_id}:recovery", event_type="RECOVERY_REQUIRED",
                          state="RECOVERY_REQUIRED", outcome="INTERRUPTED", payload={"phase": "run"})
        return store.get_run(run_id)
