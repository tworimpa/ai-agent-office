"""Operator-owned Git objects, release journal and atomic stable source selection."""

from contextlib import closing, contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile
from urllib.parse import urlsplit
from uuid import uuid4

from . import release_validation, stable_history
from .engines import resolve_executable
from .improvement_proposal import _digest, validate_proposal
from .models import validate_id
from .ownership import RunBusyError, RunGuard
from .process_window import BACKGROUND_FLAGS


_session_git = ContextVar("operator_release_git", default=None)


@contextmanager
def use_session_object_store(path):
    """Trust only the existing host-provisioned bare store for this operator session."""
    path = Path(path)
    if path.resolve(strict=True) != path:
        raise ValueError("Expected a direct path to the enrolled release object store")
    token = _session_git.set((path, path.stat()))
    try:
        yield
    finally:
        _session_git.reset(token)


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
    trusted = _session_git.get()
    if trusted and repo == trusted[0]:
        if repo.resolve(strict=True) != repo or not os.path.samestat(trusted[1], repo.stat()):
            raise ValueError("Enrolled Git object store changed during the session")
        command[1:1] = ["-c", "safe.directory=" + repo.as_posix()]
    try:
        result = subprocess.run(command, cwd=repo, env=settings, input=data or b"",
                                capture_output=True, timeout=120, creationflags=BACKGROUND_FLAGS)
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
    path, git = _paths(root)
    if not path.is_file():
        raise ValueError("Release store is not initialized")
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=30)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        begin = "BEGIN IMMEDIATE" if write else "BEGIN"
        db.execute(begin)
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == 0:
            db.rollback()
            db.execute("BEGIN IMMEDIATE")
            with db:
                if db.execute("PRAGMA user_version").fetchone()[0] == 0:
                    stable_history.migrate(db, git)
            db.execute(begin)
            version = db.execute("PRAGMA user_version").fetchone()[0]
        if version != 1:
            raise ValueError("Unsupported release store version")
        pointer = db.execute("SELECT 1 FROM release WHERE slot=1 AND stable="
                             "(SELECT sha FROM stable_versions ORDER BY seq DESC LIMIT 1)").fetchone()
        if pointer is None:
            raise ValueError("Stable pointer disagrees with version history")
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
    when = datetime.now(timezone.utc).isoformat()
    db.execute("INSERT INTO history(candidate,time,event,details) VALUES(?,?,?,?)",
               (id, when, event, json.dumps(details)))
    return when


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
            stable_history.initialize(db, sha)
    return {"stable": sha, "image": image}


def _fingerprint(image, encoded, first_seq, base_seq):
    # Keep legacy fingerprints on the first visit to a SHA. A new submission
    # after rollback belongs to a fresh review generation.
    digest = hashlib.sha256(image.encode("ascii") + b"\0" + encoded.encode("utf-8"))
    if first_seq != base_seq:
        digest.update(b"\0" + str(base_seq).encode("ascii"))
    return digest.hexdigest()


def _admitted(db, id, job):
    if job is not None:
        db.execute("INSERT INTO studio_imports VALUES(?,?,?,?)", (*job, id))
    return _candidate(db, id)


def submit(root, proposal, image=None, *, studio_job=None, prior_candidate=None, expected_release_id=None):
    proposal = validate_proposal(proposal)
    encoded = json.dumps(proposal, sort_keys=True, ensure_ascii=False, allow_nan=False)
    _, git = _paths(root)
    with _db(root, write=True) as db:
        if expected_release_id is not None:
            stable_history.identity(db, expected_release_id)
        release = _release(db)
        if image is None:
            image = release["image"]
        _check_image(image)
        base = proposal["base"]
        first_seq, base_seq = db.execute("SELECT min(seq),max(seq) FROM stable_versions WHERE sha=?",
                                         (base,)).fetchone()
        if studio_job is not None:
            journal_id, job_id, artifact_digest = studio_job
            validate_id(journal_id)
            validate_id(job_id)
            if artifact_digest != _digest(proposal):
                raise ValueError("Studio job digest disagrees with the proposal")
            db.execute("""CREATE TABLE IF NOT EXISTS studio_imports(
                journal TEXT NOT NULL, job TEXT NOT NULL, digest TEXT NOT NULL,
                candidate TEXT NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
                PRIMARY KEY(journal,job,digest)) WITHOUT ROWID""")
            bound = db.execute("SELECT candidate FROM studio_imports WHERE journal=? AND job=? AND digest=?",
                               studio_job).fetchone()
            candidate = bound["candidate"] if bound else prior_candidate
            if candidate is not None:
                prior = db.execute("SELECT proposal,image,fingerprint,base_seq FROM candidates WHERE id=?",
                                   (validate_id(candidate),)).fetchone()
                if prior is None:
                    raise ValueError("Candidate missing from release. Inspect the restored release store.")
                if (prior["proposal"] != encoded or prior["image"] != image
                        or prior["fingerprint"] != _fingerprint(image, encoded, first_seq, prior["base_seq"])):
                    raise ValueError("Studio import disagrees with its candidate")
                return _candidate(db, candidate) if bound else _admitted(db, candidate, studio_job)
        fingerprint = _fingerprint(image, encoded, first_seq, base_seq)
        existing = db.execute("SELECT id FROM candidates WHERE fingerprint=?", (fingerprint,)).fetchone()
        if existing:
            return _admitted(db, existing["id"], studio_job)
        stable = release["stable"]
        if base != stable:
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
        db.execute("INSERT INTO candidates(id,fingerprint,base,sha,state,proposal,image,base_seq) "
                   "VALUES(?,?,?,?,?,?,?,?)",
                   (id, fingerprint, stable, sha, "submitted", encoded, image, base_seq))
        _log(db, id, "submitted", {"base": stable, "sha": sha, "image": image})
        return _admitted(db, id, studio_job)


def _check_review_reference(reference):
    if (not isinstance(reference, str) or not 1 <= len(reference) <= 512
            or any(not char.isascii() or char.isspace() or not char.isprintable() for char in reference)):
        raise ValueError("Specify a bounded HTTPS review URL")
    address = urlsplit(reference)
    if (address.scheme != "https" or not address.hostname or address.username is not None
            or not address.path.strip("/") or address.query or address.fragment):
        raise ValueError("Specify a HTTPS review URL without credentials, query or fragment")


def _revision_source(proposal, sha):
    source = proposal.get("source")
    if source is None:
        return None
    if (not isinstance(source, dict) or set(source) != {"kind", "revision", "reference"}
            or source["kind"] != "git_revision" or source["revision"] != sha or proposal.get("history") != []):
        raise ValueError("Git review provenance disagrees with the candidate")
    _check_review_reference(source["reference"])
    return source


def import_revision(root, repo, revision, reference, *, image=None):
    """Admit an operator-selected Git commit without rewriting its objects or stable pointer."""
    if not isinstance(revision, str) or not release_validation.COMMIT_ID.fullmatch(revision):
        raise ValueError("Specify a full commit SHA from a trusted checkout")
    _check_review_reference(reference)
    repo = Path(repo).expanduser().resolve(strict=True)
    top = Path(os.fsdecode(run_git(repo, "rev-parse", "--show-toplevel").rstrip(b"\r\n"))).resolve()
    if run_git(top, "cat-file", "-t", revision).strip() != b"commit":
        raise ValueError("The selected revision must be a commit")
    _, git = _paths(root)
    with _db(root, write=True) as db:
        stable = stable_history.latest(db)
        base, seq = stable["sha"], stable["seq"]
        if revision == base:
            raise ValueError("This commit is already stable")
        image = _release(db)["image"] if image is None else image
        _check_image(image)
        proposal = {"title": f"운영자 Git 커밋 {revision[:12]}",
                    "rationale": f"운영자가 지정한 검토 출처 {reference}",
                    "expected": "검토한 Git 커밋의 소스를 그대로 승격합니다.", "history": [],
                    "source": {"kind": "git_revision", "revision": revision, "reference": reference}}
        encoded = json.dumps(proposal, sort_keys=True, ensure_ascii=False, allow_nan=False)
        fingerprint = hashlib.sha256(f"git-revision\0{seq}\0{base}\0{image}\0{encoded}".encode("utf-8")).hexdigest()
        existing = db.execute("SELECT id FROM candidates WHERE fingerprint=?", (fingerprint,)).fetchone()
        if existing:
            return _candidate(db, existing["id"])
        stable_history._check_ref(db, git, stable)
        run_git(git, "fetch", "--no-write-fetch-head", "--no-tags", "--no-recurse-submodules", "--", top.as_uri(), revision)
        try:
            run_git(git, "merge-base", "--is-ancestor", base, revision)
        except ValueError:
            raise ValueError("The selected Git revision must descend from the current stable commit") from None
        release_validation.committed_files(git, revision)
        if not _diff(git, base, revision, allow_binary=True):
            raise ValueError("The selected Git revision makes no source changes")
        cid = uuid4().hex
        run_git(git, "update-ref", "refs/candidates/" + cid, revision)
        db.execute("INSERT INTO candidates(id,fingerprint,base,sha,state,proposal,image,base_seq) VALUES(?,?,?,?,?,?,?,?)",
                   (cid, fingerprint, base, revision, "submitted", encoded, image, seq))
        _log(db, cid, "submitted", {"base": base, "sha": revision, "image": image, "source": proposal["source"]})
        return _candidate(db, cid)


def _diff(git, base, sha, *, allow_binary=False):
    flags = () if allow_binary else ("--text",)
    raw = run_git(git, "diff", *flags, "--full-index", "--no-ext-diff", "--no-textconv",
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
    return review_with_version(root, id)[0]


def review_with_version(root, id):
    _, git = _paths(root)
    with _db(root) as db:
        result = _candidate(db, id)
        proposal, base_seq = db.execute("SELECT proposal,base_seq FROM candidates WHERE id=?", (id,)).fetchone()
        history = [dict(event=row["event"], time=row["time"], details=json.loads(row["details"]))
                   for row in db.execute("SELECT * FROM history WHERE candidate=? ORDER BY seq", (id,))]
        current = dict(stable_history.latest(db))
        release_id = stable_history.identity(db)
        result["stable"] = current["sha"]
        result["stale_base"] = (result["state"] not in ("approved", "rejected")
                                and (base_seq, result["base"]) != (current["seq"], current["sha"]))
    proposal = json.loads(proposal)
    source = _revision_source(proposal, result["sha"])
    result.update(proposal=proposal, diff=_diff(git, result["base"], result["sha"], allow_binary=source is not None),
                  history=history, validation=next((row["details"] for row in reversed(history)
                                                     if row["event"] == "validation_finished"), None))
    return result, current, base_seq, release_id


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


def verify_candidate(root, id, *, first_attempt_only=False, on_started=None, session_report=None):
    """Record an exact-tree validation. Only the explicit operator command can retry."""
    _, git = _paths(root)
    with _guard(root, id):
        with _db(root, write=True) as db:
            record = _candidate(db, id)
            if first_attempt_only and (record["state"] != "submitted" or db.execute(
                    "SELECT 1 FROM history WHERE candidate=? AND event='validation_started' LIMIT 1",
                    (id,)).fetchone()):
                return None
            if record["state"] == "passed":
                return review(root, id)
            if record["state"] not in {"submitted", "failed"} or not _current_base(db, id):
                raise ValueError("Candidate is terminal or its base is stale")
            sha, image = record["sha"], record["image"]
            _log(db, id, "validation_started", {"sha": sha, "image": image})
        if on_started is not None:
            on_started()
        try:
            report = (release_validation.validate_candidate(git, sha, image)
                      if session_report is None else session_report)
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


def _current_base(db, id):
    current = stable_history.latest(db)
    return db.execute("SELECT 1 FROM candidates WHERE id=? AND base_seq=? AND base=?",
                      (id, current["seq"], current["sha"])).fetchone() is not None


def decide(root, id, sha, decision, *, reviewed_version=None, expected_release_id=None):
    if decision not in {"approve", "reject"}:
        raise ValueError("Decision must be approve or reject")
    state = "approved" if decision == "approve" else "rejected"
    with _guard(root, id), _db(root, write=True) as db:
        stable_history.identity(db, expected_release_id)
        if reviewed_version is not None and stable_history.version(db, reviewed_version["seq"]) != reviewed_version:
            raise ValueError("Stable version changed during review")
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
            if not _current_base(db, id):
                raise ValueError("Candidate base differs from current stable source")
            if reviewed_version is not None and reviewed_version["seq"] != stable_history.latest(db)["seq"]:
                raise ValueError("Stable version changed during review")
        db.execute("UPDATE candidates SET state=? WHERE id=?", (state, id))
        when = _log(db, id, state, {"sha": sha, "base": record["base"]})
        if decision == "approve":
            stable_history.advance(db, "promotion", sha, when=when, candidate_id=id)
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
