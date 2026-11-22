"""Operator-owned authorization for the next fully attested main merge.

This module has no Studio HTTP route. Unattended execution consumes an existing
enrollment, never input from a coding session, webhook or candidate script.
"""

from contextlib import closing, contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
from uuid import uuid4

from . import deployment, policy_github, release_store, runtime_ops, service_binding, stable_history
from .ownership import RunGuard


DB = "operator-policy.sqlite3"
URL = "https://github.com/" + policy_github.REPOSITORY + ".git"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


@contextmanager
def _state(root, *, create=False, write=False):
    path = Path(root).resolve() / DB
    if path.is_symlink() or (not create and not path.is_file()):
        raise ValueError("Register an operator deployment policy first")
    mode = "rwc" if create else "rw" if write else "ro"
    with closing(sqlite3.connect(path.as_uri() + "?mode=" + mode, uri=True, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN IMMEDIATE" if create or write else "BEGIN")
        with db:
            if create:
                db.execute("""CREATE TABLE IF NOT EXISTS policies(target TEXT PRIMARY KEY,
                    generation TEXT NOT NULL, enabled INTEGER NOT NULL, binding TEXT NOT NULL)""")
                db.execute("""CREATE TABLE IF NOT EXISTS intents(target TEXT NOT NULL, generation TEXT NOT NULL,
                    sha TEXT NOT NULL, base_seq INTEGER NOT NULL, base TEXT NOT NULL, candidate TEXT NOT NULL,
                    evidence TEXT NOT NULL, predecessor TEXT NOT NULL, operation_id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL, PRIMARY KEY(target,generation,sha))""")
                db.execute("""CREATE TABLE IF NOT EXISTS audit(seq INTEGER PRIMARY KEY, at TEXT NOT NULL,
                    target TEXT NOT NULL, generation TEXT NOT NULL, action TEXT NOT NULL, details TEXT NOT NULL)""")
            yield db


def _audit(db, target, generation, action, details):
    db.execute(
        "INSERT INTO audit(at,target,generation,action,details) VALUES(?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), target, generation, action, _json(details)),
    )


def binding(root, target, image):
    release_store._check_image(image)
    target = Path(target).resolve(strict=True)
    config, _ = deployment.read_selection(target)
    with release_store._db(root) as db:
        stable_history.identity(db, config["release_id"])
    native = service_binding.inspect_service(config, target)
    journal = Path(native["journal_path"]).resolve(strict=True)

    def file_identity(path):
        info = path.stat()
        return [info.st_dev, info.st_ino]

    return dict(
        repository=policy_github.REPOSITORY,
        branch="main",
        image=image,
        release=str(Path(root).resolve(strict=True)),
        target=str(target),
        target_id=config["target_id"],
        release_id=config["release_id"],
        journal_id=config["journal_id"],
        journal=str(journal),
        journal_file=file_identity(journal),
        port=config["port"],
        target_file=file_identity(target / deployment.DB_NAME),
        release_file=file_identity(Path(root) / "release.sqlite3"),
        manager=config["manager"],
        service=config["service"],
        signature=native["signature"],
    )


def enroll(root, target, reviewed):
    # Bind the consent to the actual registration seen in the enrollment UI.
    if binding(root, target, reviewed["image"]) != reviewed:
        raise ValueError("Deployment target changed during enrollment")
    healthy = deployment.public_status(target)
    current = stable_history.page(root, limit=1)["current"]
    if (
        healthy["health"] != "healthy"
        or not healthy["actual"]
        or (healthy["actual"]["seq"], healthy["actual"]["sha"]) != (current["seq"], current["sha"])
    ):
        raise ValueError("Enrollment requires the healthy running stable version")
    ident = reviewed["target_id"]
    with _state(root, create=True) as db:
        previous = db.execute("SELECT * FROM policies WHERE target=?", (ident,)).fetchone()
        if previous and previous["enabled"]:
            if json.loads(previous["binding"]) == reviewed:
                return dict(state="enabled", generation=previous["generation"], binding=reviewed)
            raise ValueError("Disable the previous policy before changing its binding")
        gen = uuid4().hex
        db.execute("INSERT OR REPLACE INTO policies VALUES(?,?,1,?)", (ident, gen, _json(reviewed)))
        _audit(db, ident, gen, "enabled", reviewed)
    return dict(state="enabled", generation=gen, binding=reviewed)


def _policy(db, ident):
    row = db.execute("SELECT * FROM policies WHERE target=?", (ident,)).fetchone()
    if not row:
        raise ValueError("No operator policy for this target")
    return dict(
        generation=row["generation"], enabled=bool(row["enabled"]), binding=json.loads(row["binding"])
    )


def status(root, target):
    config, _ = deployment.read_selection(target)
    with _state(root) as db:
        policy = _policy(db, config["target_id"])
        rows = db.execute(
            "SELECT at,generation,action,details FROM audit WHERE target=? ORDER BY seq DESC LIMIT 24",
            (config["target_id"],),
        )
        policy["history"] = [dict(row) | {"details": json.loads(row["details"])} for row in rows]
        return policy


def disable(root, target):
    config, _ = deployment.read_selection(target)
    ident = config["target_id"]
    with _state(root, write=True) as db:
        policy = _policy(db, ident)
        if policy["enabled"]:
            db.execute("UPDATE policies SET enabled=0 WHERE target=?", (ident,))
            _audit(db, ident, policy["generation"], "disabled", {})
        return {"state": "disabled", "target_id": ident}


def _mirror(root):
    """Fetch objects from the enrolled public repository into an operator-owned Git directory."""
    mirror = Path(root).resolve() / "auto-main"
    if not mirror.exists():
        with release_store._publish_directory(mirror) as stage:
            release_store.run_git(stage, "init", "-q")
            release_store.run_git(stage, "remote", "add", "origin", URL)
    if (
        mirror.is_symlink()
        or release_store.run_git(mirror, "remote", "get-url", "origin").decode().strip() != URL
    ):
        raise ValueError("Operator mirror origin changed")
    # No ambient git configuration, credential helpers, submodules or workspace checkout.
    # Anonymous HTTPS suffices for this public repository. Refuse rewritten history.
    release_store.run_git(
        mirror,
        "-c",
        "http.followRedirects=false",
        "-c",
        "credential.helper=",
        "fetch",
        "--no-tags",
        "--no-recurse-submodules",
        "--no-write-fetch-head",
        "--",
        URL,
        "refs/heads/main:refs/heads/policy-fetch",
        env={"GIT_ALLOW_PROTOCOL": "https"},
    )
    tip = release_store.run_git(mirror, "rev-parse", "refs/heads/policy-fetch").decode().strip()
    release_store.run_git(mirror, "update-ref", "refs/remotes/origin/main", tip)
    return mirror


def _next(mirror, base):
    tip = (
        release_store.run_git(mirror, "rev-parse", "--verify", "refs/remotes/origin/main^{commit}")
        .decode()
        .strip()
    )
    if base == tip:
        return None
    release_store.run_git(mirror, "merge-base", "--is-ancestor", base, tip)
    commits = release_store.run_git(
        mirror, "rev-list", "--first-parent", "--reverse", base + ".." + tip
    ).splitlines()
    if not commits:
        raise ValueError("Stable revision is not on the main first-parent chain")
    revision = commits[0].decode("ascii")
    if release_store.run_git(mirror, "rev-parse", revision + "^1").decode().strip() != base:
        raise ValueError("Stable revision is not the candidate's main parent")
    return revision


def _insist(db, ident, expected):
    if _policy(db, ident) != expected:
        raise ValueError("Deployment policy was revoked or replaced")


def _pending(db, ident, gen):
    return db.execute(
        """SELECT * FROM intents WHERE target=? AND generation=? AND state!='healthy'
        ORDER BY rowid DESC LIMIT 1""",
        (ident, gen),
    ).fetchone()


def _predecessor(review):
    actual = review["observed"]["probe"]["identity"]
    return dict(
        selection=review["selected"],
        last=review["last"]["id"] if review["last"] else None,
        boot=actual["boot_id"] if actual else None,
        signature=review["observed"]["binding"]["signature"],
    )


def _finish(db, intent, native_state):
    state = "healthy" if native_state in {"healthy", "unchanged"} else "needs_operator"
    key = (intent["target"], intent["generation"], intent["sha"])
    db.execute("UPDATE intents SET state=? WHERE target=? AND generation=? AND sha=?", (state, *key))
    result = dict(
        state=state,
        service_outcome=native_state,
        sha=intent["sha"],
        seq=intent["base_seq"] + 1,
        operation=intent["operation_id"],
        candidate=intent["candidate"],
    )
    _audit(db, *key[:2], state, result)
    return result


def _settle_started(root, target, policy, intent):
    """Only finish or restore our own already recorded operation, including when GitHub is offline."""
    b = policy["binding"]
    _, _, op = deployment._snapshot(target)
    if not op or op["id"] != intent["operation_id"]:
        return None
    seq = intent["base_seq"] + 1
    if (op["requested"]["seq"], op["requested"]["sha"], op["signature"]) != (
        seq,
        intent["sha"],
        b["signature"],
    ):
        raise ValueError("Service operation disagrees with the durable policy intent")
    with _state(root, write=True) as db:
        _insist(db, b["target_id"], policy)
        if binding(root, target, b["image"]) != b:
            raise ValueError("Deployment target changed during recovery")
        if op["state"] in {"deploy_failed_restored", "deploy_failed_stopped", "restore_failed"}:
            return _finish(db, intent, op["state"])
        if op["state"] == "healthy":
            running = deployment.public_status(target)
            if running["health"] != "healthy" or running["actual"]["activation_id"] != op["id"]:
                raise ValueError("Cannot confirm the policy boot")
            return _finish(db, intent, "healthy")
        if op["state"] not in deployment.PENDING:
            raise ValueError("Unknown policy operation state")
        with runtime_ops.idle_journal(Path(b["journal"]), b["journal_id"]) as idle:
            review = deployment.review(root, target, seq, recover=True)
            if review["last"] != op or review["versions"]["current"] != review["versions"]["requested"]:
                raise ValueError("Manual release or service decision requires operator recovery")
            result = deployment.apply_review(root, target, review, release_idle=idle)
            return _finish(db, intent, result["state"])


def _run(root, target, policy):
    b = policy["binding"]
    ident, gen = b["target_id"], policy["generation"]
    if binding(root, target, b["image"]) != b:
        raise ValueError("Operator policy no longer matches the registered service and journal")
    with _state(root) as db:
        pending = _pending(db, ident, gen)
    if pending and pending["state"] == "needs_operator":
        return dict(state="needs_operator", sha=pending["sha"])
    if pending:
        recovered = _settle_started(root, target, policy, pending)
        if recovered is not None:
            return recovered
    current = stable_history.page(root, limit=1)["current"]
    mirror = _mirror(root)
    base = pending["base"] if pending else current["sha"]
    seq = pending["base_seq"] if pending else current["seq"]
    sha = _next(mirror, base)
    if pending and sha != pending["sha"]:
        raise ValueError("Pending authorized merge is no longer next on main")
    if not sha:
        actual = deployment.public_status(target)
        if (
            actual["health"] != "healthy"
            or not actual["actual"]
            or (actual["actual"]["seq"], actual["actual"]["sha"]) != (seq, base)
        ):
            raise ValueError("Stable version and running service differ")
        return dict(state="unchanged", sha=base)
    if not pending:
        # Defer early for busy studios; retry atomically under the final idle lease below.
        with runtime_ops.idle_journal(Path(b["journal"]), b["journal_id"]):
            pass
        proof = policy_github.attest(b["repository"], sha, base, mirror)
        ref = f"https://github.com/{b['repository']}/pull/{proof.pr}"
        record = release_store.import_revision(root, mirror, sha, ref, image=b["image"])
        if record["state"] == "submitted":
            record = release_store.verify_candidate(root, record["id"])
        if record["state"] != "passed" or (record["sha"], record["base"], record["image"]) != (
            sha,
            base,
            b["image"],
        ):
            raise ValueError("Exact candidate requires a successful isolated validation")
        cid = record["id"]
        prior = deployment.review(root, target, seq)
        if (
            prior["versions"]["current"] != prior["versions"]["requested"]
            or not prior["selected"]
            or (prior["selected"]["seq"], prior["selected"]["sha"]) != (seq, base)
        ):
            raise ValueError("The verified stable predecessor must still be running")
        origin = _json(_predecessor(prior))
        operation = uuid4().hex
        with _state(root, write=True) as db:
            _insist(db, ident, policy)
            db.execute(
                "INSERT INTO intents VALUES(?,?,?,?,?,?,?,?,?,'authorized')",
                (ident, gen, sha, seq, base, cid, _json(asdict(proof)), origin, operation),
            )
            _audit(
                db,
                ident,
                gen,
                "authorized",
                dict(candidate=cid, base_seq=seq, base=base, operation=operation, **asdict(proof)),
            )
            pending = _pending(db, ident, gen)
    else:
        cid = pending["candidate"]
    # Refresh external evidence after expensive validation and again on crash recovery.
    proof = policy_github.attest(b["repository"], sha, base, mirror)
    rec, current, base_seq, release_id = release_store.review_with_version(root, cid)
    if (
        (rec["sha"], rec["base"], base_seq, release_id, rec["image"])
        != (sha, base, seq, b["release_id"], b["image"])
        or (rec["proposal"].get("source") or {}).get("reference")
        != f"https://github.com/{b['repository']}/pull/{proof.pr}"
        or not rec.get("validation")
        or not rec["validation"]["passed"]
    ):
        raise ValueError("Stored candidate does not match policy evidence")
    with (
        _state(root, write=True) as db,
        runtime_ops.idle_journal(Path(b["journal"]), b["journal_id"]) as idle,
    ):
        _insist(db, ident, policy)
        if binding(root, target, b["image"]) != b:
            raise ValueError("Target changed since attestation")
        prior = deployment.review(root, target, seq)
        if _predecessor(prior) != json.loads(pending["predecessor"]):
            raise ValueError("Running predecessor or manual service decision changed since authorization")
        if (current["seq"], current["sha"], rec["state"]) == (seq, base, "passed"):
            release_store.decide(
                root, cid, sha, "approve", reviewed_version=current, expected_release_id=b["release_id"]
            )
        elif (current["seq"], current["sha"], current["candidate_id"], rec["state"]) != (
            seq + 1,
            sha,
            cid,
            "approved",
        ):
            raise ValueError("Stable version changed since policy authorization")
        now_stable = stable_history.page(root, limit=1)["current"]
        if (now_stable["seq"], now_stable["sha"], now_stable["candidate_id"]) != (seq + 1, sha, cid):
            raise ValueError("Promotion did not select the attested candidate")
        reviewed = deployment.review(root, target, seq + 1)
        if reviewed["versions"]["current"] != now_stable or reviewed["versions"]["requested"] != now_stable:
            raise ValueError("Operator changed the stable version before deployment")
        if _predecessor(reviewed) != json.loads(pending["predecessor"]):
            raise ValueError("Registered service or live predecessor changed")
        _audit(db, ident, gen, "attested", asdict(proof))
        result = deployment.apply_review(
            root, target, reviewed, release_idle=idle, operation_id=pending["operation_id"]
        )
        return _finish(db, pending, result["state"])


def run_once(root, target):
    config, _ = deployment.read_selection(target)
    ident = config["target_id"]
    with RunGuard(Path(root).resolve() / DB, ident), _state(root) as db:
        policy = _policy(db, ident)
        # End the read transaction before a separate connection records the intent.
        db.commit()
        if not policy["enabled"]:
            return dict(state="disabled", target_id=ident)
        try:
            return _run(root, target, policy)
        except runtime_ops.JournalBusy:
            return dict(state="busy", target_id=ident)
        except (ValueError, sqlite3.Error, OSError, RuntimeError, subprocess.SubprocessError) as exc:
            with _state(root, write=True) as log:
                previous = log.execute(
                    "SELECT action,details FROM audit WHERE target=? ORDER BY seq DESC LIMIT 1", (ident,)
                ).fetchone()
                reason = {"reason": str(exc)[:600]}
                if not previous or (previous["action"], previous["details"]) != ("refused", _json(reason)):
                    _audit(log, ident, policy["generation"], "refused", reason)
            raise
