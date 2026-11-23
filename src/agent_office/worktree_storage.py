"""Operator-selected cleanup of journal-owned, merged, clean Studio worktrees."""
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import threading
from uuid import uuid4

from .engines import resolve_executable
from .models import ACTIVE, OfficeError, validate_id
from .ownership import RunBusyError, RunGuard
from .release_validation import _execute, _tool_env
from .store import TaskStore
from .studio_catalog import _local_directory, _redirected
from .workspace import Worktree, plan_worktree

RETENTION = timedelta(days=7)
REASONS = {"active", "recovery", "review", "checkpoint", "candidate", "pending", "child",
           "recent", "locked", "unknown", "dirty", "unmerged", "identity", "git_locked",
           "missing", "linked", "changed", "partial", "git_version", "interrupted_move", "interrupted_drain"}
GIT_OPERATIONS = ("rebase-merge", "rebase-apply", "sequencer", "MERGE_HEAD", "CHERRY_PICK_HEAD",
                  "REVERT_HEAD", "BISECT_START")
OID = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})")


def route(method, path):
    match = re.fullmatch(r"/rooms/([a-f0-9]{32})/storage", path)
    return match[1] if match and method in {"GET", "POST"} else None


def schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS studio_worktree_cleanup(
        run_id TEXT PRIMARY KEY REFERENCES runs(run_id), head TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS studio_worktree_staging(
        run_id TEXT PRIMARY KEY REFERENCES runs(run_id), folder TEXT NOT NULL,
        directory_id TEXT NOT NULL, checkout_id TEXT NOT NULL, moved INTEGER NOT NULL DEFAULT 0)""")


def _git_command(repo, args):
    # Honor checkout line-ending and file-mode settings.
    env = _tool_env()
    env.update({k: os.environ[k] for k in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME") if k in os.environ})
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_ALLOW_PROTOCOL="", GIT_NO_LAZY_FETCH="1", GIT_NO_REPLACE_OBJECTS="1")
    git = resolve_executable("git.exe" if os.name == "nt" else "git")
    if not git:
        raise ValueError("Git unavailable")
    return [git, "-C", str(repo), "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
            "-c", "core.untrackedCache=false", "-c", "gc.auto=0", *args], env


def _git(repo, *args):
    argv, env = _git_command(repo, args)
    code, output = _execute(argv, env=env, limit=8 * 1024 * 1024, timeout=30, stderr=subprocess.DEVNULL)
    if code:
        raise ValueError("Git worktree state unavailable")
    return output


def _registered_roots(catalog, workspace):
    folders = catalog.get(workspace)["folders"]
    roots = set()
    for index in range(len(folders)):
        try:
            repo, _ = catalog.select(workspace, [index])
            roots.add(repo)
        except ValueError:
            continue
    return roots


def _mounts_below(root):
    # Linux reports same-device bind mounts here even when ismount() is false.
    if os.name == "nt":
        return os.path.ismount(root)
    with open("/proc/self/mountinfo", "rb") as stream:
        for row in stream:
            fields = row.split()
            if len(fields) < 6:
                raise ValueError("Missing mount information")
            mount = re.sub(rb"\\([0-7]{3})", lambda match: bytes([int(match[1], 8)]), fields[4])
            if Path(os.fsdecode(mount)).is_relative_to(root):
                return True
    return False


def _usage(root):
    total = 0
    directories = [root]
    if _mounts_below(root):
        return None, True
    linked = False
    device = root.stat().st_dev
    while directories:
        directory = Path(directories.pop())
        if directory.resolve(strict=True) != directory or _redirected(directory.lstat()):
            linked = True
            continue
        with os.scandir(directory) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                changed_device = os.name != "nt" and info.st_dev != device
                if _redirected(info) or changed_device:
                    linked = True
                elif stat.S_ISDIR(info.st_mode):
                    directories.append(entry.path)
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
                else:
                    linked = True
    return total, linked


def _inventory(repo):
    worktrees = {}
    try:
        records = _git(repo, "worktree", "list", "--porcelain", "-z")
        separator = b"\0"
    except ValueError:
        # Older Git lacks -z. Reject ambiguous multi-line paths or reasons.
        records = _git(repo, "worktree", "list", "--porcelain")
        separator = b"\n"
    for record in records.strip(separator).split(separator * 2):
        fields = {}
        for field in record.split(separator):
            key, _, value = field.partition(b" ")
            if key not in {b"worktree", b"HEAD", b"branch", b"bare", b"detached", b"locked", b"prunable"} or key in fields:
                raise ValueError("Ambiguous worktree registration")
            fields[key] = value
        if b"worktree" in fields:
            worktrees[Path(fields[b"worktree"].decode("utf-8"))] = fields
    return worktrees


def _references(db, run, tables):
    queries = {
        "checkpoint": "SELECT 1 FROM studio_goal_checkpoints WHERE run_id=?",
        "pending": """SELECT 1 FROM studio_requests WHERE run_id=?1 AND status IN ('pending','decided')
            UNION ALL SELECT 1 FROM studio_goal_queue WHERE run_id=?1
            UNION ALL SELECT 1 FROM studio_goal_stage_waits WHERE run_id=?1 AND ended_at IS NULL
            UNION ALL SELECT 1 FROM workspace_dependencies dep JOIN runs r ON dep.run_id=r.run_id
                WHERE dep.source_run=?1 AND (r.ended_at IS NULL OR r.state='RECOVERY_REQUIRED')
            UNION ALL SELECT 1 FROM studio_meetings WHERE run_id=?1 AND status='awaiting_review'""",
        "child": """SELECT 1 FROM studio_goal_workers WHERE run_id=?1 AND ended_at IS NULL
            UNION ALL SELECT 1 FROM studio_team_children c JOIN studio_team_children sibling USING(parent_id)
            JOIN runs r ON r.run_id=sibling.id WHERE c.id=?1 AND r.ended_at IS NULL""",
    }
    if "studio_goal_candidates" in tables:
        queries["candidate"] = """SELECT 1 FROM studio_goal_candidates c LEFT JOIN studio_goals g ON g.id=c.run_id
            WHERE c.run_id=? AND (g.adopted_attempt IS NULL OR c.attempt>g.adopted_attempt)"""
    if "studio_improvements" in tables:
        queries["candidate"] = (queries.get("candidate", "SELECT 1 WHERE 0") +
            " UNION ALL SELECT 1 FROM studio_improvements i,json_each(i.run_ids) j WHERE j.value=?1 AND i.state IN ('generating','ready')")
    return [reason for reason, query in queries.items() if db.execute(query, (run,)).fetchone()]


_ROWS = """SELECT r.run_id,r.state,r.ended_at,t.project,t.workspace,t.branch,t.current_run_id,
    g.adopted_attempt,x.head AS cleanup_head,q.folder,q.directory_id,q.checkout_id,q.moved
    FROM runs r JOIN tasks t ON t.task_id=r.task_id LEFT JOIN studio_runs s ON s.id=r.run_id
    LEFT JOIN studio_goals g ON g.id=r.run_id LEFT JOIN studio_worktree_cleanup x ON x.run_id=r.run_id
    LEFT JOIN studio_worktree_staging q ON q.run_id=r.run_id
    WHERE r.host_name=? AND (s.workspace_id=? OR (s.id IS NULL AND t.project IN ({roots})))"""


def _file_id(path):
    if _local_directory(str(path)) != path:
        raise ValueError("Redirected directory")
    info = path.stat()
    if not info.st_ino:
        raise ValueError("No stable file identity")
    return f"{info.st_dev}:{info.st_ino}"


def _new_quarantine(tree):
    parent = tree.path.parent / (".office-cleanup-" + uuid4().hex)
    _local_directory(str(parent.parent))
    if os.name == "nt":
        import pywintypes
        import win32api
        import win32con
        import win32file
        import win32security
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            sid, _ = win32security.GetTokenInformation(token, win32security.TokenUser)
        finally:
            token.Close()
        owner = win32security.ConvertSidToStringSid(sid)
        attributes = pywintypes.SECURITY_ATTRIBUTES()
        attributes.SECURITY_DESCRIPTOR = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
            f"D:P(A;OICI;FA;;;{owner})", win32security.SDDL_REVISION_1)
        win32file.CreateDirectory(str(parent), attributes)
    else:
        parent.mkdir(mode=0o700)
    (parent / "registered").mkdir()
    (parent / "gate").write_bytes(parent.name.encode("ascii"))
    return parent


def _restore_interrupted_move(staged, original):
    # The OS rename must never replace a newly created original directory.
    if os.name == "nt":
        os.rename(staged, original)
    else:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, "renameat2", None)
        if not rename or rename(-100, os.fsencode(staged), -100, os.fsencode(original), 1) != 0:
            raise OSError("Original checkout path is unavailable")


def _staging(tree, row):
    name = row["folder"]
    if not isinstance(name, str) or not re.fullmatch(r"\.office-cleanup-[a-zA-Z0-9_-]{6,64}", name):
        raise ValueError("Invalid quarantine directory")
    parent = tree.path.parent / name
    if os.path.lexists(parent) and _file_id(parent) != row["directory_id"]:
        raise ValueError("Quarantine was replaced")
    return Worktree(parent / "registered" / "checkout", tree.branch)


def _inspect(row, roots, db, tables, owners, inventory):
    run = row["run_id"]
    reasons = _references(db, run, tables)
    result = {"run_id": run, "state": row["state"], "bytes": None, "head": None, "reasons": reasons}
    if row["state"] in ACTIVE or not row["ended_at"]:
        reasons.append("active")
    elif row["state"] == "RECOVERY_REQUIRED":
        reasons.append("recovery")
    elif row["state"] == "WAITING_USER" or row["state"] == "READY_FOR_HUMAN_REVIEW" and row["adopted_attempt"] is None:
        reasons.append("review")
    elif row["state"] not in {"FAILED", "PLAN_ONLY", "CANCELED", "READY_FOR_HUMAN_REVIEW"}:
        reasons.append("unknown")
    if row["ended_at"] and datetime.fromisoformat(row["ended_at"]) > datetime.now(timezone.utc) - RETENTION:
        reasons.append("recent")
    if run in owners:
        reasons.append("child")
    repo = Path(row["project"])
    if repo not in roots or row["current_run_id"] != run:
        reasons.append("identity")
        return result
    tree = plan_worktree(repo, run)
    if tree.path != Path(row["workspace"]) or tree.branch != row["branch"]:
        reasons.append("identity")
        return result
    if repo not in inventory:
        inventory[repo] = _inventory(repo)
    records = inventory[repo]
    if row["folder"]:
        staged = _staging(tree, row)
        if os.path.lexists(staged.path):
            result["location"] = str(staged.path)
            if _file_id(staged.path) != row["checkout_id"]:
                reasons.append("identity")
                return result
            if row["moved"] == 2 and not os.path.lexists(staged.path / ".git") and (staged.path.parents[1] / "pointer").is_file():
                result["head"] = row["cleanup_head"]
                reasons.append("interrupted_drain")
                return result
            if staged.path not in records and tree.path in records and not row["moved"]:
                result["head"] = row["cleanup_head"]
                reasons.append("identity" if os.path.lexists(tree.path) else "interrupted_move")
                return result
            tree = staged
        elif row["moved"]:
            if os.path.lexists(staged.path.parents[1]):
                result["location"] = str(staged.path.parents[1])
            # Never adopt a directory subsequently created at the original name.
            tree = staged
        elif not os.path.lexists(tree.path) or _file_id(tree.path) != row["checkout_id"]:
            reasons.append("identity")
            return result
    entry = records.get(tree.path)
    ref = "refs/heads/" + tree.branch
    missing = not os.path.lexists(tree.path)
    if missing and row["moved"] == 2 and entry and entry.get(b"branch") == ref.encode():
        # Only the quarantined checkout has disappeared. Its known registration
        # is still pending retirement. A regular-file parent will guard Git.
        result.update(head=row["cleanup_head"], bytes=0)
    elif entry is None and missing:
        if row["cleanup_head"]:
            refs = _git(repo, "for-each-ref", "--format=%(objectname)", ref).decode().splitlines()
            if not refs:
                return None
            if refs == [row["cleanup_head"]] and all(item.get(b"branch") != ref.encode() for item in records.values()):
                result.update(head=refs[0], bytes=0)
            else:
                reasons.append("identity")
        else:
            reasons.append("missing")
    elif not entry or entry.get(b"branch") != ref.encode():
        reasons.append("identity")
    else:
        _inspect_checkout(repo, tree, entry, result, draining=row["moved"] == 2)
    if result["head"]:
        if not OID.fullmatch(result["head"]):
            raise ValueError("Unknown Git revision")
        if _git(repo, "merge-base", "HEAD", result["head"]).strip().decode("ascii") != result["head"]:
            reasons.append("unmerged")
    return result


def _git_pointer(path, prefix=b""):
    if _redirected(path.lstat()) or not path.is_file():
        raise ValueError("Invalid Git metadata")
    with path.open("rb") as stream:
        marker = stream.read(8193)
    if len(marker) > 8192 or not marker.startswith(prefix):
        raise ValueError("Invalid Git pointer")
    return (path.parent / marker[len(prefix):].decode("utf-8").rstrip("\r\n")).resolve(strict=True)


def _inspect_checkout(repo, tree, entry, result, *, draining=False):
    reasons = result["reasons"]
    if b"locked" in entry or b"prunable" in entry:
        reasons.append("git_locked")
    result["_checkout_id"] = _file_id(tree.path)
    common = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip())
    dotgit = tree.path / ".git"
    admin = _git_pointer(dotgit, b"gitdir: ")
    if (admin.parent != common / "worktrees" or _local_directory(str(admin)) != admin
            or _git_pointer(admin / "gitdir") != dotgit or _git_pointer(admin / "commondir") != common):
        reasons.append("identity")
        return
    if any(os.path.lexists(admin / marker) for marker in GIT_OPERATIONS):
        reasons.append("git_locked")
    result["head"] = entry[b"HEAD"].decode("ascii")
    result["bytes"], linked = _usage(tree.path)
    if linked:
        reasons.append("linked")
        return
    changes = _git(tree.path, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored",
                   "--ignore-submodules=none")
    if draining:
        changes = any(field and field[:3] not in (b"D  ", b" D ") for field in changes.split(b"\0"))
    index = _git(tree.path, "ls-files", "-v", "--stage", "-z")
    if changes or any(item and not item.startswith((b"H 100644 ", b"H 100755 ")) for item in index.split(b"\0")):
        reasons.append("dirty")


@contextmanager
def _hold_checkout(repo, admin, ref, *, integration=None):
    # Cross-worktree HEAD names are readable but Git rejects them in update-ref
    # transactions. Each checkout therefore owns its HEAD lock via its own Git
    # transaction. EOF aborts and releases the lock even if Studio terminates.
    pointer = admin / "HEAD"
    if _redirected(pointer.lstat()) or not pointer.is_file():
        raise ValueError("Unknown checkout HEAD")
    with pointer.open("rb") as stream:
        head = stream.read(8193).rstrip(b"\r\n")
    if len(head) > 8192 or any(os.path.lexists(admin / op) for op in GIT_OPERATIONS):
        raise ValueError("Checkout operation in progress")
    if integration:
        if head != b"ref: " + integration[b"branch"] or head[5:] == ref:
            raise ValueError("Integration checkout changed")
        # Verifying its current branch locks both the branch and its HEAD.
        assertion = b"verify " + integration[b"branch"] + b"\0" + integration[b"HEAD"]
    elif head.startswith(b"ref: refs/heads/") and head[5:] != ref:
        head_ref = head[5:].decode("utf-8")
        oid = _git(repo, "--git-dir=" + str(admin), "rev-parse", "--verify", head_ref).strip()
        if not OID.fullmatch(oid.decode("ascii")):
            raise ValueError("Unknown checkout revision")
        assertion = b"verify " + head[5:] + b"\0" + oid
    elif OID.fullmatch(head.decode("ascii")):
        assertion = b"verify HEAD\0" + head
    else:
        raise ValueError("Branch in use or unsupported HEAD")
    argv, env = _git_command(repo, ["--git-dir=" + str(admin), "update-ref", "--stdin", "--no-deref", "-z"])
    with subprocess.Popen(argv, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as git:
        timeout = threading.Timer(10, git.kill)
        timeout.start()
        try:
            git.stdin.write(b"start\0" + assertion + b"\0prepare\0")
            git.stdin.flush()
            if git.stdout.readline(64).strip() != b"start: ok" or git.stdout.readline(64).strip() != b"prepare: ok":
                raise ValueError("Checkout HEAD changed or Git cannot lock symbolic refs")
            timeout.cancel()
            if not (admin / "HEAD.lock").is_file():
                raise ValueError("Git did not lock the checkout HEAD")
            yield git
        finally:
            timeout.cancel()
            git.stdin.close()


def _delete_merged_branch(repo, branch, expected):
    ref = ("refs/heads/" + branch).encode("ascii")
    attached = _inventory(repo)
    base = attached.get(repo, {})
    if b"branch" not in base or any(row.get(b"branch") == ref for row in attached.values()):
        raise ValueError("Branch in use or no attached integration checkout")
    if _git(repo, "merge-base", base[b"HEAD"].decode("ascii"), expected).strip() != expected.encode():
        raise ValueError("Branch no longer merged")
    common = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip())
    _local_directory(str(common))
    admins = [common]
    directory = common / "worktrees"
    if directory.exists():
        _local_directory(str(directory))
        admins.extend(sorted(_local_directory(str(admin)) for admin in directory.iterdir()))
    if len(admins) != len(attached):
        raise ValueError("Worktree registration changed")
    checkout_admin = Path(_git(repo, "rev-parse", "--absolute-git-dir").decode().strip())
    if checkout_admin not in admins:
        raise ValueError("Unregistered integration checkout")
    with ExitStack() as stack:
        guards = [stack.enter_context(_hold_checkout(repo, admin, ref, integration=base if admin == checkout_admin else None))
                  for admin in admins]
        if _inventory(repo) != attached or any(guard.poll() is not None for guard in guards):
            raise ValueError("Checkout registration or lock changed")
        name = ref.decode("ascii")
        _git(repo, "update-ref", "--no-deref", "-d", name, expected)
        # Git has no repository-wide lock against creating a new linked
        # checkout. Restore this tip only if its name is still absent when
        # registration changes across the deletion.
        try:
            if _inventory(repo) != attached:
                raise ValueError("Concurrent worktree registration")
        except (ValueError, OSError):
            _git(repo, "update-ref", "--no-deref", name, expected, "0" * len(expected))
            raise


def _drain_quarantine(repo, staged, row, db):
    """Only Git-tracked clean files and known control files may be unlinked."""
    checkout = staged.path
    registry = checkout.parent
    private = registry.parent
    pointer = private / "pointer"
    # The journal is durable before the first index or working-file mutation.
    db.execute("UPDATE studio_worktree_staging SET moved=2 WHERE run_id=? AND moved=1", (row["run_id"],))
    if os.path.lexists(checkout):
        if _file_id(checkout) != row["checkout_id"] or _mounts_below(checkout):
            return "identity"
        _git(checkout, "rm", "-qr", "--ignore-unmatch", "--", ":/")
        marker = checkout / ".git"
        if any(entry != marker for entry in checkout.iterdir()):
            return "dirty"
        _restore_interrupted_move(marker, pointer)
        try:
            # A process retaining its old cwd can still add ignored files.
            # rmdir refuses any such entry and never traverses a directory.
            checkout.rmdir()
        except OSError:
            _restore_interrupted_move(pointer, marker)
            return "dirty"
    if os.path.lexists(private):
        if _file_id(private) != row["directory_id"]:
            return "identity"
        if row["moved"] == 3 and not os.path.lexists(registry):
            private.rmdir()
            return None
        if registry.is_dir() and not _redirected(registry.lstat()):
            registry.rmdir()
        # With a non-directory parent, even a simultaneous creation of a
        # replacement original or writes from an old cwd cannot cause Git to
        # recursively remove files. Never pass any other path to worktree remove.
        if not os.path.lexists(registry):
            _restore_interrupted_move(private / "gate", registry)
        info = registry.lstat()
        if _redirected(info) or not stat.S_ISREG(info.st_mode) or info.st_size != len(row["folder"]):
            return "identity"
        if registry.read_bytes() != row["folder"].encode("ascii"):
            return "identity"
        if os.path.lexists(pointer):
            # Verify the preserved Git marker while the registration still
            # exists. Unexpected files in private control slots are retained.
            info = pointer.lstat()
            if _redirected(info) or not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
                return "identity"
            contents = pointer.read_bytes().rstrip(b"\r\n")
            if not contents.startswith(b"gitdir: "):
                return "identity"
            admin = (checkout / contents[8:].decode("utf-8")).resolve()
            common = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip())
            if admin.parent != common / "worktrees" or _local_directory(str(admin)) != admin:
                return "identity"
            backlink = admin / "gitdir"
            if _redirected(backlink.lstat()) or not backlink.is_file() or backlink.stat().st_size > 8192:
                return "identity"
            advertised = (admin / backlink.read_text(encoding="utf-8").rstrip("\r\n")).resolve()
            if advertised != checkout / ".git" or not os.path.samestat(info, pointer.lstat()):
                return "identity"
            pointer.unlink()
        if checkout in _inventory(repo):
            _git(repo, "worktree", "remove", "--", str(checkout))
        db.execute("UPDATE studio_worktree_staging SET moved=3 WHERE run_id=?", (row["run_id"],))
        registry.unlink()
        private.rmdir()
    return None


def _owners(runner):
    with runner.lock:
        return (set(runner.children) | set(runner.pending) | set(runner.goal_prompts)
                | runner.team_workers | runner.queued_goals | runner.stale)


def _row_status(row, roots, store, tables, owners, inventory, *, held=False):
    run = row["run_id"]
    try:
        item = _inspect(row, roots, store.connection, tables, owners, inventory)
        if item and not held and (RunGuard(store.path, run).is_held() or RunGuard.worker(store.path, run).is_held()):
            item["reasons"].append("locked")
        if item:
            item["reasons"] = list(dict.fromkeys(item["reasons"]))
            if not held:
                item.pop("_checkout_id", None)
        return item
    except (OSError, ValueError, KeyError, TypeError):
        return {"run_id": run, "state": row["state"], "bytes": None, "head": None, "reasons": ["unknown"]}


def handle(server, workspace, data):
    roots = _registered_roots(server.catalog, workspace)
    if data is not None:
        if set(data) != {"run_id", "head", "journal_id"} or data["journal_id"] != server.journal_id:
            raise ValueError("정리할 작업실 저널과 실행을 다시 선택하세요.")
        run = validate_id(data["run_id"])
        if not isinstance(data["head"], str) or not OID.fullmatch(data["head"]):
            raise ValueError("정리할 작업 트리의 커밋을 확인하세요.")
    with TaskStore(server.db_path, readonly=data is None, existing=True) as store:
        db = store.connection
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        rows_sql = _ROWS.format(roots=",".join("?" for _ in roots) or "NULL")
        scope = (platform.node(), workspace, *map(str, roots))
        inventory = {}
        if data is None:
            owners = _owners(server.runner)
            rows = db.execute(rows_sql + " ORDER BY r.started_at DESC,r.run_id", scope).fetchall()
            items = [item for row in rows if (item := _row_status(row, roots, store, tables, owners, inventory)) is not None]
            return {"workspace_id": workspace, "journal_id": server.journal_id, "entries": items}
        select = rows_sql + " AND r.run_id=?"
        if db.execute(select, (*scope, run)).fetchone() is None:
            raise OfficeError("NOT_FOUND")
        with ExitStack() as guards:
            try:
                guards.enter_context(RunGuard(server.db_path, run))
                guards.enter_context(RunGuard.worker(server.db_path, run))
            except RunBusyError:
                return _receipt(server, workspace, run, "locked")
            row = db.execute(select, (*scope, run)).fetchone()
            if row is None:
                raise OfficeError("NOT_FOUND")
            item = _row_status(row, roots, store, tables, _owners(server.runner), inventory, held=True)
            if item is None:
                outcome = "removed" if row["cleanup_head"] == data["head"] else "changed"
                return _receipt(server, workspace, run, outcome)
            repo = Path(row["project"])
            tree = plan_worktree(repo, run)
            if item["head"] == data["head"] and item["reasons"] == ["interrupted_drain"]:
                try:
                    staged = _staging(tree, row)
                    _restore_interrupted_move(staged.path.parents[1] / "pointer", staged.path / ".git")
                    item = _row_status(row, roots, store, tables, _owners(server.runner), {}, held=True)
                except (ValueError, OSError):
                    return _receipt(server, workspace, run, "partial")
            if item["head"] == data["head"] and item["reasons"] == ["interrupted_move"]:
                try:
                    _restore_interrupted_move(_staging(tree, row).path, tree.path)
                    item = _row_status(row, roots, store, tables, _owners(server.runner), {}, held=True)
                except (ValueError, OSError):
                    return _receipt(server, workspace, run, "partial")
            if item["reasons"] or item["head"] != data["head"]:
                return _receipt(server, workspace, run, (item["reasons"] or ["changed"])[0])
            try:
                if _local_directory(str(repo)) != repo:
                    return _receipt(server, workspace, run, "identity")
                if row["folder"] is None and os.path.lexists(tree.path):
                    # Exercise the host's checkout lock capability before moving
                    # any data, including with older Git or another ref backend.
                    try:
                        base = inventory.get(repo, {}).get(repo) or _inventory(repo)[repo]
                        admin = Path(_git(repo, "rev-parse", "--absolute-git-dir").decode().strip())
                        with _hold_checkout(repo, admin, ("refs/heads/" + tree.branch).encode(), integration=base):
                            pass
                    except (ValueError, OSError, KeyError):
                        return _receipt(server, workspace, run, "git_version")
                    checkout_id = item["_checkout_id"]
                    if _file_id(tree.path) != checkout_id:
                        return _receipt(server, workspace, run, "identity")
                    parent = _new_quarantine(tree)
                    try:
                        with store._write():
                            db.execute("INSERT OR REPLACE INTO studio_worktree_cleanup VALUES (?,?)", (run, data["head"]))
                            db.execute("INSERT INTO studio_worktree_staging VALUES (?,?,?,?,0)",
                                       (run, parent.name, _file_id(parent), checkout_id))
                    except BaseException:
                        (parent / "gate").unlink()
                        (parent / "registered").rmdir()
                        parent.rmdir()
                        raise
                    row = db.execute(select, (*scope, run)).fetchone()
                if row["folder"]:
                    staged = _staging(tree, row)
                    if not row["moved"]:
                        if not os.path.lexists(staged.path):
                            if _file_id(tree.path) != row["checkout_id"] or _mounts_below(tree.path):
                                return _receipt(server, workspace, run, "identity")
                            _git(repo, "worktree", "move", "--", str(tree.path), str(staged.path))
                        if _file_id(staged.path) != row["checkout_id"] or staged.path not in _inventory(repo):
                            return _receipt(server, workspace, run, "identity")
                        db.execute("UPDATE studio_worktree_staging SET moved=1 WHERE run_id=?", (run,))
                    row = db.execute(select, (*scope, run)).fetchone()
                    checked = _row_status(row, roots, store, tables, _owners(server.runner), {}, held=True)
                    if checked is None:
                        return _receipt(server, workspace, run, "removed")
                    if checked["reasons"] or checked["head"] != data["head"]:
                        return _receipt(server, workspace, run, (checked["reasons"] or ["changed"])[0])
                    if reason := _drain_quarantine(repo, staged, row, db):
                        return _receipt(server, workspace, run, reason)
                _delete_merged_branch(repo, tree.branch, data["head"])
            except (ValueError, OSError):
                return _receipt(server, workspace, run, "partial")
            return _receipt(server, workspace, run, "removed")


def _receipt(server, workspace, run, outcome):
    return {"workspace_id": workspace, "journal_id": server.journal_id, "run_id": run, "outcome": outcome}


def check_reply(status, data, workspace, journal, payload):
    from .studio_ssh import RemoteUnavailable
    if status != 200 or data.get("workspace_id") != workspace or data.get("journal_id") != journal:
        raise RemoteUnavailable()
    if payload is not None:
        if data.get("run_id") != payload.get("run_id") or data.get("outcome") not in REASONS | {"removed"}:
            raise RemoteUnavailable()
        return
    items = data.get("entries")
    if not isinstance(items, list):
        raise RemoteUnavailable()
    seen = set()
    for item in items:
        if (not isinstance(item, dict) or not isinstance(item.get("run_id"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", item["run_id"]) or item["run_id"] in seen
                or not isinstance(item.get("state"), str) or not isinstance(item.get("reasons"), list)
                or "location" in item and (not isinstance(item["location"], str) or len(item["location"]) > 32768)
                or any(type(val) is not str or val not in REASONS for val in item["reasons"])
                or item.get("bytes") is not None and (type(item["bytes"]) is not int or item["bytes"] < 0)
                or item.get("head") is not None and not OID.fullmatch(str(item["head"]))
                or not item["reasons"] and not item.get("head")):
            raise RemoteUnavailable()
        seen.add(item["run_id"])
