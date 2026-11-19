"""Operator-owned Git objects, release journal and atomic stable source selection."""

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
from uuid import uuid4

from . import release_validation
from .engines import resolve_executable
from .improvement_proposal import validate_proposal
from .models import validate_id
from .ownership import RunBusyError, RunGuard


def run_git(repo: Path, *args: str, data: bytes | None = None,
            env: dict[str, str] | None = None) -> bytes:
    """Run fixed Git plumbing with local file transport and no ambient Git configuration."""
    settings = {k: v for k, v in os.environ.items()
                if k.upper() in {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "TMPDIR"}}
    settings.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                    GIT_ATTR_NOSYSTEM="1", GIT_TEMPLATE_DIR="", GIT_NO_REPLACE_OBJECTS="1",
                    GIT_NO_LAZY_FETCH="1", GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL="file",
                    GIT_AUTHOR_NAME="Agent Office", GIT_AUTHOR_EMAIL="release@localhost",
                    GIT_COMMITTER_NAME="Agent Office", GIT_COMMITTER_EMAIL="release@localhost",
                    LC_ALL="C")
    settings.update(env or {})
    git = resolve_executable("git.exe" if os.name == "nt" else "git")
    if not git:
        raise ValueError("Git is required")
    command = [git, "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
               "-c", "core.autocrlf=false", "-c", f"core.attributesFile={os.devnull}",
               "-c", "core.protectNTFS=true", "-c", "core.protectHFS=true",
               "-c", "gc.auto=0", *args]
    try:
        result = subprocess.run(command, cwd=repo, env=settings, input=data or b"",
                                capture_output=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("Git operation timed out") from exc
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", errors="replace").strip()[:2000]
                         or "Git operation failed")
    return result.stdout


_SCHEMA = """
CREATE TABLE release (slot INTEGER PRIMARY KEY CHECK(slot=1), stable TEXT NOT NULL, image TEXT NOT NULL);
CREATE TABLE candidates (
    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL UNIQUE, base TEXT NOT NULL,
    sha TEXT NOT NULL, state TEXT NOT NULL, proposal TEXT NOT NULL, image TEXT NOT NULL
);
CREATE TABLE history (
    seq INTEGER PRIMARY KEY, candidate TEXT NOT NULL REFERENCES candidates(id),
    time TEXT NOT NULL, event TEXT NOT NULL, details TEXT NOT NULL
);
CREATE INDEX history_candidate ON history(candidate, seq);
"""


def _paths(root):
    directory = Path(root).expanduser().resolve()
    return directory / "release.sqlite3", directory / "objects.git"


def _new_path(name):
    path = Path(name).expanduser()
    # A dangling symlink is an occupied output name, even if its target is absent.
    if path.is_symlink():
        raise FileExistsError(f"Choose a new directory: {path}")
    return path.resolve()


@contextmanager
def _publish_directory(dest):
    """Publish the entire directory atomically, even if another creator races us."""
    if os.path.lexists(dest):
        raise FileExistsError(f"Choose a new directory: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=dest.parent, prefix=".office-stage-") as parent:
        stage = Path(parent) / "contents"
        stage.mkdir(mode=0o700)
        yield stage
        if os.name == "nt":
            # Windows rename refuses an occupied destination.
            stage.rename(dest)
        else:
            # Plain POSIX rename replaces an existing empty directory. Linux
            # renameat2 with RENAME_NOREPLACE atomically checks even that case.
            import ctypes
            rename = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
            if rename is None:
                raise OSError("Atomic directory publication requires Linux renameat2")
            if rename(-100, os.fsencode(stage), -100, os.fsencode(dest), 1):
                code = ctypes.get_errno()
                raise OSError(code, os.strerror(code), dest)


@contextmanager
def _db(root, *, write=False):
    path, _ = _paths(root)
    if not path.is_file():
        raise ValueError("Release store is not initialized")
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=30)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        with db:
            yield db


def _release(db):
    return db.execute("SELECT stable,image FROM release WHERE slot=1").fetchone()


def release_status(root):
    with _db(root) as db:
        return dict(_release(db))


def _candidate(db, id):
    row = db.execute("SELECT id,sha,base,state,image FROM candidates WHERE id=?",
                     (validate_id(id),)).fetchone()
    if row is None:
        raise ValueError("Unknown candidate")
    return dict(row)


def _log(db, id, event, details):
    db.execute("INSERT INTO history(candidate,time,event,details) VALUES(?,?,?,?)",
               (id, datetime.now(timezone.utc).isoformat(), event, json.dumps(details)))


def _check_image(image):
    if not isinstance(image, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("Validation requires a pinned sha256 image ID")


def init_release(root, repo, revision, image):
    _check_image(image)
    if not isinstance(revision, str) or not revision or revision.startswith("-"):
        raise ValueError("Expected a Git revision")
    root, repo = _new_path(root), Path(repo).expanduser().resolve(strict=True)
    top = Path(os.fsdecode(run_git(repo, "rev-parse", "--show-toplevel").rstrip(b"\r\n"))).resolve()
    common = Path(os.fsdecode(run_git(repo, "rev-parse", "--path-format=absolute",
                                      "--git-common-dir").rstrip(b"\r\n"))).resolve()
    worktrees = run_git(repo, "worktree", "list", "--porcelain", "-z").split(b"\0")
    checkouts = [Path(os.fsdecode(field[9:])).resolve() for field in worktrees if field.startswith(b"worktree ")]
    if root.is_relative_to(common) or any(root.is_relative_to(path) or path.is_relative_to(root)
                                         for path in checkouts):
        raise ValueError("Choose a release directory outside the source checkout and Git directory")
    sha = run_git(repo, "rev-parse", "--verify", "--end-of-options", revision + "^{commit}").strip().decode("ascii")
    object_format = run_git(repo, "rev-parse", "--show-object-format").strip().decode("ascii")
    with _publish_directory(root) as stage:
        database, git = _paths(stage)
        git.mkdir()
        run_git(git, "init", "--bare", "--template=", "--object-format=" + object_format)
        run_git(git, "fetch", "--no-tags", "--no-recurse-submodules", "--",
                top.as_uri(), sha + ":refs/releases/initial")
        release_validation.committed_files(git, sha)
        with closing(sqlite3.connect(database)) as db, db:
            db.executescript(_SCHEMA)
            db.execute("INSERT INTO release VALUES(1,?,?)", (sha, image))
    return {"stable": sha, "image": image}


def submit(root, proposal, image=None):
    proposal = validate_proposal(proposal)
    encoded = json.dumps(proposal, sort_keys=True, ensure_ascii=False, allow_nan=False)
    _, git = _paths(root)
    with _db(root, write=True) as db:
        release = _release(db)
        if image is None:
            image = release["image"]
        _check_image(image)
        fingerprint = hashlib.sha256(image.encode("ascii") + b"\0" + encoded.encode("utf-8")).hexdigest()
        existing = db.execute("SELECT id FROM candidates WHERE fingerprint=?", (fingerprint,)).fetchone()
        if existing:
            return _candidate(db, existing["id"])
        stable = release["stable"]
        if proposal["base"] != stable:
            raise ValueError("Proposal base differs from current stable source")
        patch = proposal["patch"].encode("utf-8")
        if b"\nGIT binary patch\n" in patch:
            raise ValueError("Binary patches are unsupported")
        with tempfile.TemporaryDirectory(dir=git.parent) as temp:
            index = {"GIT_INDEX_FILE": str(Path(temp) / "index")}
            run_git(git, "read-tree", stable, env=index)
            run_git(git, "apply", "--cached", "--whitespace=nowarn", "-", data=patch, env=index)
            tree = run_git(git, "write-tree", env=index).strip().decode("ascii")
        if tree == run_git(git, "rev-parse", stable + "^{tree}").strip().decode("ascii"):
            raise ValueError("Patch makes no changes")
        sha = run_git(git, "commit-tree", tree, "-p", stable, "-m",
                      "Improvement proposal " + fingerprint).strip().decode("ascii")
        release_validation.committed_files(git, sha)
        _diff(git, stable, sha)  # Confirm the complete UTF-8 review is available before admission.
        id = uuid4().hex
        # Pin in Git before committing a SQL record referencing these immutable objects.
        run_git(git, "update-ref", "refs/candidates/" + id, sha)
        db.execute("INSERT INTO candidates VALUES(?,?,?,?,?,?,?)",
                   (id, fingerprint, stable, sha, "submitted", encoded, image))
        _log(db, id, "submitted", {"base": stable, "sha": sha, "image": image})
        return _candidate(db, id)


def _diff(git, base, sha):
    raw = run_git(git, "diff", "--text", "--full-index", "--no-ext-diff", "--no-textconv",
                  "--no-color", "--no-renames", "--ignore-submodules=none", base, sha, "--")
    if b"\0" in raw:
        raise ValueError("The complete change must be reviewable as text")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("The complete change must be reviewable as UTF-8") from exc


def list_candidates(root):
    with _db(root) as db:
        return [dict(row) for row in db.execute("SELECT id,sha,base,state,image FROM candidates ORDER BY rowid")]


def review(root, id):
    _, git = _paths(root)
    with _db(root) as db:
        result = _candidate(db, id)
        proposal = db.execute("SELECT proposal FROM candidates WHERE id=?", (id,)).fetchone()[0]
        history = [dict(event=row["event"], time=row["time"], details=json.loads(row["details"]))
                   for row in db.execute("SELECT * FROM history WHERE candidate=? ORDER BY seq", (id,))]
        result["stable"] = _release(db)["stable"]
    result.update(proposal=json.loads(proposal), diff=_diff(git, result["base"], result["sha"]),
                  history=history, validation=next((row["details"] for row in reversed(history)
                                                     if row["event"] == "validation_finished"), None))
    return result


@contextmanager
def _guard(root, id):
    path, _ = _paths(root)
    if not path.is_file():
        raise ValueError("Release store is not initialized")
    try:
        with RunGuard(path, id):
            yield
    except RunBusyError as exc:
        raise ValueError("Another operation is active for this candidate") from exc


def verify_candidate(root, id):
    _, git = _paths(root)
    with _guard(root, id):
        with _db(root, write=True) as db:
            record = _candidate(db, id)
            release = _release(db)
            if record["state"] == "passed":
                return review(root, id)
            if record["state"] not in {"submitted", "failed"} or release["stable"] != record["base"]:
                raise ValueError("Candidate is terminal or its base is stale")
            sha, image = record["sha"], record["image"]
            _log(db, id, "validation_started", {"sha": sha, "image": image})
        try:
            report = release_validation.validate_candidate(git, sha, image)
            if (not isinstance(report, dict) or report.get("sha") != sha or report.get("image") != image
                    or type(report.get("passed")) is not bool or not isinstance(report.get("output"), str)
                    or ("exit_code" not in report or (report["exit_code"] is not None
                                                       and type(report["exit_code"]) is not int))
                    or (report["passed"] and report["exit_code"] != 0)):
                raise ValueError("Invalid validation result or candidate identity")
        except Exception as exc:
            report = {"passed": False, "sha": sha, "image": image, "exit_code": None,
                      "output": f"Validation failed: {exc}"[:2000]}
        with _db(root, write=True) as db:
            db.execute("UPDATE candidates SET state=? WHERE id=?",
                       ("passed" if report["passed"] else "failed", id))
            _log(db, id, "validation_finished", report)
        return review(root, id)


def decide(root, id, sha, decision):
    if decision not in {"approve", "reject"}:
        raise ValueError("Decision must be approve or reject")
    state = "approved" if decision == "approve" else "rejected"
    with _guard(root, id), _db(root, write=True) as db:
        record = _candidate(db, id)
        if sha != record["sha"]:
            raise ValueError("Expected SHA differs from candidate")
        if state == record["state"]:
            return record
        if record["state"] in {"approved", "rejected"}:
            raise ValueError("Candidate already has a different final decision")
        if decision == "approve":
            if record["state"] != "passed":
                raise ValueError("Only a passed candidate can be approved")
            updated = db.execute("UPDATE release SET stable=? WHERE slot=1 AND stable=?",
                                 (sha, record["base"]))
            if updated.rowcount != 1:
                raise ValueError("Candidate base differs from current stable source")
        db.execute("UPDATE candidates SET state=? WHERE id=?", (state, id))
        _log(db, id, state, {"sha": sha, "base": record["base"]})
        return _candidate(db, id)


def checkout_stable(root, dest):
    with _db(root) as db:
        sha = _release(db)["stable"]
    _, git = _paths(root)
    dest = _new_path(dest)
    if dest.is_relative_to(git.parent):
        raise ValueError("Choose an export destination outside the release store")
    files = release_validation.committed_files(git, sha)
    with _publish_directory(dest) as stage:
        _export_checkout(stage, git, sha, files)
    return {"stable": sha, "destination": str(dest)}


def _export_checkout(dest, git, sha, files):
    object_format = run_git(git, "rev-parse", "--show-object-format").strip().decode("ascii")
    run_git(dest, "init", "--template=", "--initial-branch=stable", "--object-format=" + object_format)
    # Fetch only the selected ancestry without a remote, reflog of the source URL,
    # alternates, hooks or candidate refs. Populate files from the same raw blobs
    # as validation, avoiding checkout filters and platform line-ending conversion.
    run_git(dest, "fetch", "--no-tags", "--no-write-fetch-head", "--no-recurse-submodules",
            "--", git.as_uri(), sha)
    info = dest / ".git" / "info"
    info.mkdir(exist_ok=True)
    (info / "attributes").write_bytes(b"* -text -crlf -ident -filter -working-tree-encoding -eol\n")
    run_git(dest, "read-tree", sha)
    for name, mode, content in files:
        target = dest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as out:
            out.write(content)
        target.chmod(mode)
    run_git(dest, "update-ref", "refs/heads/stable", sha)
