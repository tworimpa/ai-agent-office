"""Operator-owned deployment target, verified source slots and restart journal."""

from contextlib import closing, contextmanager
from datetime import datetime, timezone
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import sys
import time
from uuid import uuid4

from . import release_store, runtime_ops, stable_history
from .models import validate_db_file, validate_id
from .ownership import RunGuard
from .release_validation import COMMIT_ID, committed_files

DB_NAME = "deployment.sqlite3"
PENDING = {"deploying", "restoring", "restore_failed"}
HEALTH_TIMEOUT = 30
POLL_INTERVAL = 0.5


def _now():
    return datetime.now(timezone.utc).isoformat()


def _schema(db):
    db.executescript("""
        CREATE TABLE config (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
        CREATE TRIGGER fixed_config BEFORE UPDATE ON config BEGIN
            SELECT RAISE(ABORT,'Deployment binding is immutable'); END;
        CREATE TRIGGER retain_config BEFORE DELETE ON config BEGIN
            SELECT RAISE(ABORT,'Deployment binding is immutable'); END;
        CREATE TABLE selection (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
        INSERT INTO selection VALUES(1,'null');
        CREATE TABLE slots (sha TEXT PRIMARY KEY, manifest TEXT NOT NULL);
        CREATE TABLE operations (seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, data TEXT NOT NULL);
    """)


def init_target(root, target, *, manager, service, port, journal_id, runtime_python=None):
    dest = release_store._new_path(target)
    root = Path(root).resolve()
    if (dest.is_relative_to(root) or root.is_relative_to(dest)
            or any((p / ".git").exists() or p.name.lower() == ".git"
                   or (p / DB_NAME).exists() or ((p / "HEAD").is_file() and (p / "objects").is_dir())
                   for p in (dest, *dest.parents))):
        raise ValueError("Choose a new deployment target outside Git, other targets and the release store")
    # Resolve the parent, preserving the venv executable name on POSIX where
    # bin/python is commonly a symlink to the system interpreter.
    executable = Path(runtime_python or sys.executable).expanduser()
    if not executable.is_absolute() or not executable.is_file():
        raise ValueError("Choose an absolute path to the trusted runtime interpreter")
    executable = executable.parent.resolve(strict=True) / executable.name
    if executable.is_relative_to(dest) or executable.is_relative_to(root):
        raise ValueError("Runtime interpreter must be outside the release and deployment target")
    with release_store._db(root) as db:
        config = dict(target_id=uuid4().hex, release_id=stable_history.identity(db),
                      journal_id=validate_id(journal_id), manager=manager, service=service, port=port,
                      runtime_python=str(executable))
    runtime_ops.validate_config(config)
    with release_store._publish_directory(dest) as stage:
        (stage / "slots").mkdir()
        with closing(sqlite3.connect(stage / DB_NAME)) as db, db:
            _schema(db)
            db.execute("INSERT INTO config VALUES(1,?)", (json.dumps(config),))
    return config


@contextmanager
def _db(target, *, write=False):
    path = Path(target).resolve(strict=True) / DB_NAME
    validate_db_file(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError("Deployment target is not initialized")
    mode = "rw" if write else "ro"
    with closing(sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True, timeout=2)) as db:
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        with db:
            yield db


def _snapshot(target):
    with _db(target) as db:
        config = json.loads(db.execute("SELECT data FROM config WHERE id=1").fetchone()[0])
        runtime_ops.validate_config(config)
        selected = json.loads(db.execute("SELECT data FROM selection WHERE id=1").fetchone()[0])
        row = db.execute("SELECT data FROM operations ORDER BY seq DESC LIMIT 1").fetchone()
    return config, selected, json.loads(row[0]) if row else None


def read_selection(target):
    """Trusted service bootstrap reads one transaction without writing to the target."""
    config, selected, _ = _snapshot(target)
    return config, selected


def _manifest(files):
    return {name: [mode, len(data), hashlib.sha256(data).hexdigest()] for name, mode, data in files}


def _plain_file(info):
    return not (getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _verify_tree(slot, manifest):
    directories = {str(parent.as_posix()) for name in manifest for parent in Path(name).parents}
    todo = [slot]
    found = set()
    while todo:
        directory = todo.pop()
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or not _plain_file(info):
            raise ValueError("Unexpected source slot directory")
        for item in directory.iterdir():
            name = item.relative_to(slot).as_posix()
            info = item.lstat()
            if not _plain_file(info):
                raise ValueError("Unexpected link in source slot")
            if stat.S_ISDIR(info.st_mode) and name in directories:
                todo.append(item)
                continue
            if name not in manifest or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Unexpected file in source slot")
            mode, size, digest = manifest[name]
            if (info.st_size != size
                    or (os.name != "nt" and stat.S_IMODE(info.st_mode) != mode)
                    or hashlib.sha256(item.read_bytes()).hexdigest() != digest):
                raise ValueError("Source slot differs from reviewed commit")
            found.add(name)
    if found != manifest.keys():
        raise ValueError("Source slot is incomplete")


def verify_slot(target, sha):
    """Check every source path, byte and executable mode against the operator's ledger."""
    if not isinstance(sha, str) or not COMMIT_ID.fullmatch(sha):
        raise ValueError("Invalid source slot")
    with _db(target) as db:
        row = db.execute("SELECT manifest FROM slots WHERE sha=?", (sha,)).fetchone()
    if row is None:
        raise ValueError("Unknown source slot")
    slots = Path(target).resolve() / "slots"
    if slots.resolve(strict=True) != slots:
        raise ValueError("Unexpected slots directory")
    slot = slots / sha
    _verify_tree(slot, json.loads(row[0]))
    return slot


def _sync_directory(path):
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


@contextmanager
def _publish_slot(slot):
    if os.name != "nt":
        with release_store._publish_directory(slot) as stage:
            yield stage
        return
    # Python 3.13's private temporary directories suppress DACL inheritance.
    # Stage beside the final slot so both it and its files inherit the slots
    # directory's provisioned ACL. Windows rename refuses an existing target.
    stage = slot.with_name(".office-stage-" + uuid4().hex)
    stage.mkdir()
    try:
        yield stage
        stage.rename(slot)
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def _stage(target, git, sha):
    files = committed_files(git, sha)
    manifest = _manifest(files)
    slots = Path(target).resolve() / "slots"
    if slots.resolve(strict=True) != slots:
        raise ValueError("Unexpected slots directory")
    slot = slots / sha
    if not os.path.lexists(slot):
        with _publish_slot(slot) as stage:
            for name, mode, data in files:
                file = stage / name
                file.parent.mkdir(parents=True, exist_ok=True)
                with file.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    file.chmod(mode)
                    os.fsync(stream.fileno())
            for directory, _, _ in os.walk(stage, topdown=False):
                Path(directory).chmod(0o755)
                _sync_directory(directory)
            _verify_tree(stage, manifest)
        _sync_directory(slots)
    else:
        _verify_tree(slot, manifest)
    with _db(target, write=True) as db:
        db.execute("INSERT OR IGNORE INTO slots VALUES(?,?)", (sha, json.dumps(manifest)))
        if json.loads(db.execute("SELECT manifest FROM slots WHERE sha=?", (sha,)).fetchone()[0]) != manifest:
            raise ValueError("Slot manifest disagrees with the reviewed Git tree")


def _pick(event, activation):
    return {"seq": event["seq"], "sha": event["sha"], "activation_id": activation}


def _matches(config, identity, selected):
    if not identity or not selected:
        return False
    return (all(identity.get(k) == config[k] for k in ("target_id", "release_id", "journal_id", "port"))
            and all(identity.get(k) == selected[k] for k in ("seq", "sha", "activation_id"))
            and validate_id(identity.get("boot_id")) == identity["boot_id"])


def _view_op(op):
    if not op:
        return None
    return {k: op[k] for k in ("id", "state", "time", "message")} | {
        k: {field: op[k][field] for field in ("seq", "sha")} if op[k] else None
        for k in ("requested", "previous")}


def public_status(target):
    """Verify the live receipt with read-only native registration and listener evidence."""
    config, selected, op = _snapshot(target)
    result = runtime_ops.probe(config)
    actual = result["identity"]
    known = op is not None and _matches(config, actual, selected)
    if op and op["state"] in PENDING and actual:
        requested = _pick(op["requested"], op["id"])
        known = known or op["previous"] == actual or _matches(config, actual, requested)
    healthy = False
    if result["state"] == "healthy" and known:
        from . import service_binding
        try:
            _check_record(config, selected, op)
            binding = service_binding.inspect_service(config, target, for_control=False)
            healthy = (binding["signature"] == op["signature"] and runtime_ops.manager_state(config) == "running"
                       and service_binding.owns_listener(binding, actual, config["port"]))
        except (ValueError, OSError, RuntimeError, sqlite3.Error):
            # A service account can lack native introspection privileges.
            healthy = False
    offline = result["state"] == "refused"
    state = "healthy" if healthy else "stopped" if offline and selected is None else "unavailable"
    if result["state"] in {"healthy", "wrong_identity"} and not healthy:
        state = "conflict"
    if op and op["state"] != "healthy":
        state = op["state"]
    public = {k: v for k, v in config.items() if k != "runtime_python"}
    return public | {"state": state, "health": "healthy" if healthy else "offline" if offline else "unverified",
                     "actual": actual if healthy else None, "selected": selected, "last": _view_op(op)}


def recent_operations(target):
    with _db(target) as db:
        rows = db.execute("SELECT data FROM operations ORDER BY seq DESC LIMIT 20")
        operations = [json.loads(row[0]) for row in rows]
    return [_view_op(op) | {key: op.get(key) for key in
            ("previous", "confirmed", "failure", "recovery_failure", "transitions")} for op in operations]


def _observed(config, target, signature=None):
    from . import service_binding
    binding = service_binding.inspect_service(config, target)
    if signature is not None and binding["signature"] != signature:
        raise ValueError("Registered native service command changed")
    probe = runtime_ops.probe(config)
    owns = False
    if probe["state"] != "refused":
        owns = service_binding.owns_listener(binding, probe["identity"], config["port"])
        if not owns:
            probe = {"state": "wrong_identity", "identity": None, "detail": "Listener outside registered service"}
    return {"manager": runtime_ops.manager_state(config), "binding": binding, "probe": probe, "owns": owns}


def _file_id(target):
    info = (Path(target) / DB_NAME).stat()
    return (info.st_dev, info.st_ino)



def _versions(root, config, seq, previous_seq=None):
    # Validate the chain and decision receipts in addition to the pinned Git ref.
    chain = stable_history.snapshot(root, release_id=config["release_id"])
    with release_store._db(root) as db:
        stable_history.identity(db, config["release_id"])
        latest = dict(stable_history.latest(db))
        wanted = stable_history.version(db, seq)
        previous = stable_history.version(db, previous_seq) if previous_seq else None
        for event in (latest, wanted, previous):
            if event:
                if event["seq"] > len(chain) or event != chain[event["seq"] - 1]:
                    raise ValueError("Stable history changed during review")
                stable_history._check_ref(db, release_store._paths(root)[1], event)
    return {"current": latest, "requested": wanted, "previous": previous}


def _check_record(config, selected, op):
    if not op:
        if selected is None:
            return
        raise ValueError("Runtime selection has no recorded deployment")
    if op["target_id"] != config["target_id"] or op["release_id"] != config["release_id"]:
        raise ValueError("Deployment record belongs to another target")
    requested = _pick(op["requested"], validate_id(op["id"]))
    previous = op["previous"]
    if previous:
        runtime_ops._receipt(config, previous)
        if not op["previous_event"] or any(previous[k] != op["previous_event"][k] for k in ("seq", "sha")):
            raise ValueError("Previous runtime receipt disagrees with the reviewed stable event")
    restored = None
    if op.get("restore_activation") and previous:
        restored = _pick(previous, validate_id(op["restore_activation"]))
    possible = {
        "deploying": [requested], "healthy": [requested], "deploy_failed_stopped": [None],
        "deploy_failed_restored": [restored] if restored else [],
        "restoring": [requested], "restore_failed": [requested],
    }
    if restored or previous is None:
        possible["restoring"].append(restored)
        possible["restore_failed"].append(restored)
    if selected not in possible.get(op["state"], []):
        raise ValueError("Runtime selection disagrees with the recorded deployment phase")


def review(root, target, seq=None, *, recover=False):
    file_id = _file_id(target)
    config, selected, op = _snapshot(target)
    _check_record(config, selected, op)
    if recover:
        if not op or op["state"] not in PENDING:
            raise ValueError("There is no unfinished deployment to recover")
        seq = op["requested"]["seq"]
        previous = op["previous"]
    else:
        if op and op["state"] in PENDING:
            raise ValueError("Recover the interrupted deployment first")
        previous = selected
    versions = _versions(root, config, seq, previous["seq"] if previous else None)
    observed = _observed(config, target, op["signature"] if recover else None)
    reply = observed["probe"]
    if recover:
        if versions["requested"] != op["requested"] or versions["previous"] != op["previous_event"]:
            raise ValueError("Recovered operation disagrees with stable history")
    elif selected:
        if (observed["manager"] != "running" or reply["state"] != "healthy"
                or not _matches(config, reply["identity"], selected)
                or selected["sha"] != versions["previous"]["sha"]):
            raise ValueError("Cannot verify the actual running predecessor of this deployment")
    elif observed["manager"] != "stopped" or reply["state"] != "refused":
        raise ValueError("First deployment requires a stopped managed service and an unused port")
    return {"config": config, "selected": selected, "last": op, "versions": versions,
            "observed": observed, "file_id": file_id, "recover": recover}


class _TargetSession:
    """Keep one target DB handle and compare its on-disk identity and last durable state."""

    def __init__(self, target, connection, reviewed, release_idle=None):
        self.path = target
        self.db = connection
        self.config = reviewed["config"]
        self.file_id = reviewed["file_id"]
        self.signature = reviewed["observed"]["binding"]["signature"]
        self.selected = deepcopy(reviewed["selected"])
        self.last = deepcopy(reviewed["last"])
        self.release_idle = release_idle

    def check(self):
        if _file_id(self.path) != self.file_id:
            raise ValueError("Deployment ledger file was replaced during the operation")
        db = self.db
        config = json.loads(db.execute("SELECT data FROM config WHERE id=1").fetchone()[0])
        prior = json.loads(db.execute("SELECT data FROM selection WHERE id=1").fetchone()[0])
        row = db.execute("SELECT data FROM operations ORDER BY seq DESC LIMIT 1").fetchone()
        last = json.loads(row[0]) if row else None
        if (config, prior, last) != (self.config, self.selected, self.last):
            raise ValueError("Deployment ledger was changed or restored during the operation")

    def store(self, op, selected):
        with self.db as db:
            db.execute("BEGIN IMMEDIATE")
            self.check()
            if self.last and self.last["id"] == op["id"]:
                db.execute("UPDATE operations SET data=? WHERE id=?", (json.dumps(op), op["id"]))
            else:
                db.execute("INSERT INTO operations(id,data) VALUES(?,?)", (op["id"], json.dumps(op)))
            db.execute("UPDATE selection SET data=? WHERE id=1", (json.dumps(selected),))
        self.selected, self.last = deepcopy(selected), deepcopy(op)

    def observe(self):
        self.check()
        observed = _observed(self.config, self.path, self.signature)
        if self.release_idle is not None:
            self.release_idle.verify(observed["binding"]["journal_path"])
        self.check()
        return observed

    def check_observed(self, expected):
        # The listener, unit command and target selection must still match the
        # immediately preceding observation, including when recovering a failure.
        if self.observe() != expected:
            raise ValueError("Native service or running predecessor changed before mutation")

    def act(self, function, expected):
        self.check_observed(expected)
        function(self.config)

    def stop(self, expected):
        if self.release_idle is None:
            return self.act(runtime_ops.stop_service, expected)
        try:
            # A failed candidate may have admitted work after its first boot.
            # Reacquire before every recovery stop; never interrupt that work.
            self.release_idle.acquire()
            self.check_observed(expected)
            if expected["manager"] != "stopped":
                runtime_ops.stop_service(self.config)
        finally:
            self.release_idle()

    def restart(self, expected):
        if self.release_idle is None:
            return self.act(runtime_ops.restart_service, expected)
        self.stop(expected)
        stopped = self.observe()
        if stopped["manager"] != "stopped" or stopped["probe"]["state"] != "refused":
            raise ValueError("Could not verify stopped service and unused port")
        self.act(runtime_ops.start_service, stopped)


def _finish(session, op, selected, state, message):
    if state.startswith("deploy_failed") and op.get("failure"):
        message += " " + op["failure"]
    op.update(state=state, time=_now(), message=message)
    op["transitions"] = (op.get("transitions", []) + [{"time": op["time"], "state": state}])[-16:]
    session.store(op, selected)
    return _view_op(op)


def _healthy(config, selected, observed, excluded):
    receipt = observed["probe"]["identity"]
    return (observed["manager"] == "running" and observed["probe"]["state"] == "healthy"
            and _matches(config, receipt, selected) and receipt["boot_id"] not in excluded)


def _failure(error):
    # Do not publish OS paths, native command output or untrusted HTTP replies.
    if isinstance(error, OSError):
        return "파일 또는 서비스 접근 실패"
    text = str(error)
    messages = {
        "Fresh boot and Studio health could not be verified": "새 부팅 또는 작업실 응답 확인 시간 초과",
        "Another runtime answered at the configured port": "등록 포트에서 다른 런타임이 응답함",
        "Recovery could not verify ownership of the service and port": "서비스 또는 포트 소유 상태 불일치",
        "Unresponsive listener is not owned by a running managed service": "중지된 서비스의 포트가 계속 사용 중",
        "A listener remained after the registered service stopped": "등록 서비스 중지 후에도 포트가 사용 중",
        "Could not verify stopped service and unused port": "서비스 중지 또는 포트 해제 확인 실패",
    }
    if text in messages:
        return messages[text]
    if "slot" in text.lower():
        return "검증된 소스 슬롯 확인 실패"
    return "서비스 관리자 명령 실패"


def _wait(session, selected, excluded):
    until = time.monotonic() + HEALTH_TIMEOUT
    while True:
        observed = session.observe()
        if _healthy(session.config, selected, observed, excluded):
            return observed["probe"]["identity"]
        if observed["probe"]["state"] == "wrong_identity":
            raise ValueError("Another runtime answered at the configured port")
        if time.monotonic() >= until:
            raise ValueError("Fresh boot and Studio health could not be verified")
        time.sleep(POLL_INTERVAL)


def _owned(config, op, selected, observed):
    """Only operate a registered service when the port is empty or belongs to this operation."""
    probe = observed["probe"]
    if observed["manager"] not in {"running", "stopped"}:
        return False
    if probe["state"] == "refused":
        return True
    if observed["manager"] == "stopped":
        return False
    if probe["state"] not in {"healthy", "unhealthy"}:
        return False
    identity = probe["identity"]
    return (bool(identity and identity == op["previous"])
            or _matches(config, identity, _pick(op["requested"], op["id"]))
            or _matches(config, identity, selected))


def _quiesce_unresponsive(session, op, selected, observed):
    # A failed boot may accept TCP without serving a receipt. Stop only the
    # registered service whose activation this operation already selected.
    # A listener that survives that stop prevents another start on this port.
    reply = observed["probe"]
    manager = observed["manager"]
    # NSSM may pause while throttling a failed child. systemd can be
    # activating with no listener while waiting to auto-restart.
    backoff = (manager in {"paused", "pausing", "continuing"} or
               (manager == "starting" and session.config["manager"] != "windows"))
    no_response = reply["state"] in {"timeout", "unhealthy"} and reply["identity"] is None
    if not (no_response or backoff):
        return observed
    identity = reply["identity"]
    if identity and not (identity == op["previous"] or _matches(session.config, identity, selected)
                         or _matches(session.config, identity, _pick(op["requested"], op["id"]))):
        raise ValueError("Recovery could not verify ownership of the service and port")
    if reply["state"] == "wrong_identity" or not ((backoff and reply["state"] == "refused") or
                                                  ((manager == "running" or backoff) and observed["owns"])):
        raise ValueError("Unresponsive listener is not owned by a running managed service")
    _finish(session, op, selected, "restoring", "등록 서비스를 중지하고 포트 해제를 확인하는 중")
    session.stop(observed)
    stopped = session.observe()
    if stopped["manager"] != "stopped" or stopped["probe"]["state"] != "refused":
        raise ValueError("A listener remained after the registered service stopped")
    return stopped


def _restore(session, op, selected):
    config = session.config
    previous = op["previous"]
    try:
        observed = _quiesce_unresponsive(session, op, selected, session.observe())
        if not _owned(config, op, selected, observed):
            raise ValueError("Recovery could not verify ownership of the service and port")
        identity = observed["probe"]["identity"]
        if _matches(config, identity, _pick(op["requested"], op["id"])):
            op["failed_boot"] = identity["boot_id"]
        if previous:
            verify_slot(session.path, previous["sha"])
            if not op.get("restore_activation"):
                op["restore_activation"] = uuid4().hex
            restored = _pick(previous, op["restore_activation"])
            excluded = {previous["boot_id"], op.get("failed_boot")}
            if not _healthy(config, restored, observed, excluded):
                if identity:
                    excluded.add(identity["boot_id"])
                selected = restored
                _finish(session, op, restored, "restoring", "직전 정상 버전으로 복원 중")
                session.restart(observed)
                op["confirmed"] = _wait(session, restored, excluded)
            else:
                op["confirmed"] = observed["probe"]["identity"]
            return _finish(session, op, restored, "deploy_failed_restored", "배포 실패 후 직전 버전의 새 부팅을 확인했습니다.")
        _finish(session, op, None, "restoring", "첫 배포 실패 후 서비스를 중지하는 중")
        selected = None
        if observed["manager"] != "stopped":
            session.stop(observed)
        check = session.observe()
        if check["manager"] != "stopped" or check["probe"]["state"] != "refused":
            raise ValueError("Could not verify stopped service and unused port")
        return _finish(session, op, None, "deploy_failed_stopped", "첫 배포 실패 후 서비스 중지를 확인했습니다.")
    except runtime_ops.JournalBusy:
        _finish(session, op, selected, "restore_failed", "작업실 사용 중으로 복원을 보류했습니다.")
        raise
    except (RuntimeError, OSError, ValueError, sqlite3.Error) as error:
        op["recovery_failure"] = _failure(error)
        return _finish(session, op, selected, "restore_failed", "운영자 복구가 필요합니다. " + op["recovery_failure"])


def apply_review(root, target, reviewed, *, release_idle=None, operation_id=None, before_operation=None):
    """Recheck the reviewed target; an idle_journal reservation guards policy-driven stops."""
    config = reviewed["config"]
    target = Path(target).resolve()
    requested = reviewed["versions"]["requested"]
    uri = (target / DB_NAME).as_uri() + "?mode=rw"
    with RunGuard(target / DB_NAME, config["target_id"]), closing(sqlite3.connect(uri, uri=True)) as connection:
        session = _TargetSession(target, connection, reviewed, release_idle)
        if release_idle is not None:
            release_idle.acquire()
        session.check()
        # Hold the release writer lock through the final review and durable intent.
        # No release transition can race confirmation or slot preparation.
        with release_store._db(root, write=True) as db:
            stable_history.identity(db, config["release_id"])
            checked = review(root, target, requested["seq"], recover=reviewed["recover"])
            if checked != reviewed:
                raise ValueError("Deployment target or running version changed during review")
            if release_idle is not None:
                release_idle.verify(checked["observed"]["binding"]["journal_path"])
            selected = checked["selected"]
            op = checked["last"]
            if not checked["recover"]:
                _stage(target, release_store._paths(root)[1], requested["sha"])
                if selected:
                    verify_slot(target, selected["sha"])
                if session.observe() != checked["observed"]:
                    raise ValueError("Running predecessor changed during source verification")
                if selected and (selected["seq"], selected["sha"]) == (requested["seq"], requested["sha"]):
                    return {"state": "unchanged", "requested": {"seq": selected["seq"], "sha": selected["sha"]}}
                if before_operation is not None:
                    before_operation()
                op = {"id": validate_id(operation_id) if operation_id is not None else uuid4().hex,
                      "target_id": config["target_id"], "release_id": config["release_id"],
                      "requested": requested, "signature": session.signature,
                      "previous": checked["observed"]["probe"]["identity"],
                      "previous_event": checked["versions"]["previous"]}
                selected = _pick(requested, op["id"])
                _finish(session, op, selected, "deploying", "선택한 버전으로 재시작 중")
        excluded = {op["previous"]["boot_id"]} if op["previous"] else set()
        if checked["recover"]:
            if op["state"] == "deploying" and _healthy(config, selected, checked["observed"], excluded):
                op["confirmed"] = checked["observed"]["probe"]["identity"]
                return _finish(session, op, selected, "healthy", "새 부팅과 작업실 응답을 확인했습니다.")
            return _restore(session, op, selected)
        try:
            session.restart(checked["observed"])
            op["confirmed"] = _wait(session, selected, excluded)
        except (RuntimeError, OSError, ValueError, sqlite3.Error) as error:
            op["failure"] = _failure(error)
            return _restore(session, op, selected)
        return _finish(session, op, selected, "healthy", "새 부팅과 작업실 응답을 확인했습니다.")
