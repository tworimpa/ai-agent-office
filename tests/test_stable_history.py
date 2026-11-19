import io
import json
import shutil
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime

import pytest

from agent_office import cli, release_store as store, release_validation, stable_history as history
from test_improvement_cli import OperatorTerminal
from test_release_store import IMAGE, proposal, release, result, source


@pytest.fixture
def passing(monkeypatch):
    monkeypatch.setattr(release_validation, "validate_candidate", lambda git, sha, image: result(sha, image))


def passed(root, change):
    candidate = store.submit(root, change)
    assert store.verify_candidate(root, candidate["id"])["state"] == "passed"
    return candidate


def approve(root, candidate):
    return store.decide(root, candidate["id"], candidate["sha"], "approve")


def test_versions_survive_restart_rollback_and_new_proposal(release, passing, tmp_path):
    root, base = release
    initial, = history.snapshot(root)
    assert initial == dict(seq=1, kind="initial", previous=None, sha=base, time=initial["time"],
                           candidate_id=None, target_seq=None)
    assert initial["time"] and "+" in initial["time"]
    first = passed(root, proposal(base))
    waiting = passed(root, proposal(base, after="alternative"))
    assert approve(root, first) == approve(root, first)
    sha = first["sha"]
    promotion = history.snapshot(root)[-1]
    assert promotion == dict(seq=2, kind="promotion", previous=base, sha=sha,
                             time=store.review(root, first["id"])["history"][-1]["time"],
                             candidate_id=first["id"], target_seq=None)
    args = dict(expected_seq=2, expected_sha=sha, target_seq=1, target_sha=base)
    review = history.review_rollback(root, 1)
    assert {k: review[k] for k in ("from", "to", "diff")} == history.compare(root, 2, 1)
    assert "-new\n+old" in review["diff"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        operations = [pool.submit(history.rollback, root, **args) for _ in range(2)]
    restored = operations[0].result()
    assert operations[1].result() == restored
    assert restored == dict(seq=3, kind="rollback", previous=sha, sha=base, time=restored["time"],
                            candidate_id=None, target_seq=1)
    assert store.checkout_stable(root, tmp_path / "restored")["stable"] == base
    assert (tmp_path / "restored" / "example.txt").read_bytes() == b"old\n"
    assert store.review(root, waiting["id"])["stale_base"] is True
    assert store.review(root, first["id"])["stale_base"] is False
    with pytest.raises(ValueError, match="base differs"):
        approve(root, waiting)
    fresh = passed(root, proposal(base, after="alternative"))
    assert store.review(root, fresh["id"])["stale_base"] is False
    assert fresh["id"] != waiting["id"] and fresh["sha"] != waiting["sha"]
    approve(root, fresh)
    assert history.snapshot(root)[:3] == [initial, promotion, restored]
    assert store.release_status(root)["stable"] == fresh["sha"]
    with pytest.raises(ValueError, match="changed"):
        history.rollback(root, **args)
    assert history.page(root, limit=2)["next_before"] == 3
    assert history.page(root, limit=2, before=3)["items"] == [promotion, initial]


def test_cli_requires_complete_review_and_an_interactive_operator(source, tmp_path, passing, monkeypatch, capsys):
    # Even when a proposal contains no control characters, a renamed baseline
    # file can supply them to the complete --no-renames diff.
    (source / "example.txt").write_bytes(b"example\x1b[2J\n")
    store.run_git(source, "commit", "-am", "Include terminal control bytes in the baseline")
    root = tmp_path / "release"
    base = store.init_release(root, source, "HEAD", IMAGE)["stable"]
    patch = ("diff --git a/example.txt b/renamed.txt\nsimilarity index 100%\n"
             "rename from example.txt\nrename to renamed.txt\n"
             "diff --git a/note.txt b/note.txt\nnew file mode 100644\n"
             "--- /dev/null\n+++ b/note.txt\n@@ -0,0 +1 @@\n+renamed example\n")
    change = passed(root, proposal(base) | {"patch": patch})
    approve(root, change)
    sha = change["sha"]
    command = ["improve", "--release", str(root)]
    assert cli.main([*command, "history", "--limit", "1"]) == 0
    assert json.loads(capsys.readouterr().out)["items"][0]["seq"] == 2
    assert cli.main([*command, "compare", "1", "2"]) == 0
    assert "+example\x1b[2J" in json.loads(capsys.readouterr().out)["diff"]
    phrase = f"rollback 2 {sha} 1 {base}"
    monkeypatch.setattr(sys, "stdin", io.StringIO(phrase + "\n"))
    assert cli.main([*command, "rollback", "1"]) == 2
    assert "interactive" in capsys.readouterr().err

    def attempt(answer):
        with monkeypatch.context() as m:
            m.setattr(sys, "stdin", OperatorTerminal(answer + "\n"))
            screen = OperatorTerminal()
            m.setattr(sys, "stderr", screen)
            code = cli.main([*command, "rollback", "1"])
            assert phrase in screen.getvalue() and "-example\\x1b[2J" in screen.getvalue()
            assert "\x1b" not in screen.getvalue()
            return code

    assert attempt(f"rollback 2 {sha} 1 {base[:8]}") == 2
    assert len(history.snapshot(root)) == 2
    assert attempt(phrase) == 0
    assert json.loads(capsys.readouterr().out)["seq"] == 3


def test_rejects_arbitrary_commit_same_sha_target_and_stale_aba_review(release, passing):
    root, base = release
    first = passed(root, proposal(base))
    other = store.submit(root, proposal(base, after="unreleased"))
    store.decide(root, other["id"], other["sha"], "reject")
    approve(root, first)
    sha = first["sha"]
    args = dict(expected_seq=2, expected_sha=sha, target_seq=1, target_sha=base)
    with pytest.raises(ValueError, match="Reviewed SHA"):
        history.rollback(root, **(args | {"target_sha": other["sha"]}))
    with pytest.raises(ValueError, match="Unknown"):
        history.rollback(root, **(args | {"target_seq": 3}))
    with pytest.raises(ValueError, match="earlier"):
        history.review_rollback(root, 2)
    history.rollback(root, **args)
    history.rollback(root, expected_seq=3, expected_sha=base, target_seq=2, target_sha=sha)
    with pytest.raises(ValueError, match="during review"):
        history.rollback(root, **args)
    with pytest.raises(ValueError, match="different commit"):
        history.review_rollback(root, 2)
    assert [e["sha"] for e in history.snapshot(root)] == [base, sha, base, sha]


def test_competing_rollback_and_promotion_share_the_same_generation(release, passing):
    root, base = release
    first = passed(root, proposal(base))
    approve(root, first)
    sha = first["sha"]
    next_candidate = passed(root, proposal(sha, before="new", after="next"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        promotion = pool.submit(approve, root, next_candidate)
        rollback = pool.submit(history.rollback, root, expected_seq=2, expected_sha=sha, target_seq=1, target_sha=base)
    errors = [operation.exception() for operation in (promotion, rollback)]
    assert errors.count(None) == 1
    assert isinstance(next(error for error in errors if error is not None), ValueError)
    versions = history.snapshot(root)
    assert len(versions) == 3
    expected = next_candidate["sha"] if promotion.exception() is None else base
    assert versions[-1]["sha"] == expected == store.release_status(root)["stable"]


@pytest.mark.parametrize("action", ["rollback", "approve"])
def test_terminal_refuses_replacement_store_with_identical_versions(release, passing, tmp_path, monkeypatch, action):
    root, base = release
    first = passed(root, proposal(base))
    approve(root, first)
    sha = first["sha"]
    if action == "rollback":
        args, phrase = ["rollback", "1"], f"rollback 2 {sha} 1 {base}"
    else:
        next_candidate = passed(root, proposal(sha, before="new", after="next"))
        args = ["approve", next_candidate["id"], "--sha", next_candidate["sha"]]
        phrase = "approve " + next_candidate["sha"]
    replacement = tmp_path / "another-release"
    original = tmp_path / "original-release"
    shutil.copytree(root, replacement)
    with closing(sqlite3.connect(replacement / "release.sqlite3")) as db, db:
        db.execute("UPDATE studio_release_identity SET id=?", ("f" * 32,))

    class SwapOnConfirmation(OperatorTerminal):
        def readline(self):
            root.rename(original)
            replacement.rename(root)
            return phrase + "\n"

    screen = OperatorTerminal()
    monkeypatch.setattr(sys, "stdin", SwapOnConfirmation())
    monkeypatch.setattr(sys, "stderr", screen)
    assert cli.main(["improve", "--release", str(root), *args]) == 2
    assert "Release identity changed" in screen.getvalue()
    assert history.snapshot(root) == history.snapshot(original)
    assert len(history.snapshot(root)) == 2


def test_restored_backup_replaying_same_sha_and_seq_invalidates_review(release, passing, tmp_path, monkeypatch):
    root, base = release
    first = passed(root, proposal(base))
    backup = tmp_path / "backup"
    shutil.copytree(root, backup)
    approve(root, first)
    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2090, 1, 1, tzinfo=tz)
    monkeypatch.setattr(store, "datetime", Later)
    approve(backup, first)
    review = history.review_rollback(root, 1)
    replay = history.review_rollback(backup, 1)
    assert replay["release_id"] == review["release_id"]
    assert replay["from"] == review["from"] | {"time": replay["from"]["time"]}
    assert replay["from"]["time"] != review["from"]["time"]
    with pytest.raises(ValueError, match="changed during review"):
        history.rollback(backup, expected_seq=2, expected_sha=first["sha"], target_seq=1, target_sha=base,
                         expected_release_id=review["release_id"], reviewed_versions=(review["from"], review["to"]))
    assert len(history.snapshot(backup)) == 2


def legacy_database(root):
    # Reconstruct the exact pre-version schema and receipts from a release fixture.
    # Its Git pins and candidate commit IDs remain the originals.
    path = root / "release.sqlite3"
    replacement = root / "legacy.sqlite3"
    with closing(sqlite3.connect(replacement)) as db, db:
        db.executescript(store._SCHEMA)
        db.execute("ATTACH DATABASE ? AS original", (str(path),))
        db.execute("INSERT INTO release SELECT * FROM original.release")
        db.execute("INSERT INTO candidates SELECT id,fingerprint,base,sha,state,proposal,image FROM original.candidates")
        db.execute("INSERT INTO history SELECT * FROM original.history")
    replacement.replace(path)


def test_migrates_legacy_receipts_in_decision_order(release, passing):
    root, base = release
    change = passed(root, proposal(base))
    stale = passed(root, proposal(base, after="unreviewed"))
    rejected = store.submit(root, proposal(base, after="rejected"))
    store.decide(root, rejected["id"], rejected["sha"], "reject")
    approve(root, change)
    second = passed(root, proposal(change["sha"], before="new", after="next"))
    approve(root, second)
    original = history.snapshot(root)
    receipts = store.review(root, change["id"])["history"]
    legacy_database(root)
    recovered = history.snapshot(root)
    assert recovered == [original[0] | {"time": None}, *original[1:]]
    assert store.review(root, change["id"])["history"] == receipts
    assert store.submit(root, proposal(base))["id"] == change["id"]
    history.rollback(root, expected_seq=3, expected_sha=second["sha"], target_seq=1, target_sha=base)
    with pytest.raises(ValueError, match="base differs"):
        approve(root, stale)
    assert store.submit(root, proposal(base, after="unreviewed"))["id"] != stale["id"]


@pytest.mark.parametrize("fault", ["pointer", "receipt", "state", "ref", "missing_approval"])
def test_broken_migration_leaves_original_database_intact(release, passing, fault):
    root, base = release
    change = passed(root, proposal(base))
    approve(root, change)
    legacy_database(root)
    path = root / "release.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        if fault == "pointer":
            db.execute("UPDATE release SET stable=?", (base,))
        elif fault == "receipt":
            db.execute("UPDATE history SET details='{}' WHERE event='approved'")
        elif fault == "state":
            db.execute("UPDATE candidates SET state='rejected'")
        elif fault == "ref":
            store.run_git(root / "objects.git", "update-ref", "refs/candidates/" + change["id"], base)
        else:
            db.execute("DELETE FROM history WHERE event='approved'")
    with pytest.raises(ValueError):
        history.snapshot(root)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("PRAGMA user_version").fetchone() == (0,)
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='stable_versions'").fetchone()


def test_interrupted_transition_and_legacy_writer_are_atomic(release, passing):
    root, base = release
    change = passed(root, proposal(base))
    path = root / "release.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        with pytest.raises(sqlite3.IntegrityError, match="version record"):
            db.execute("UPDATE release SET stable=?", (change["sha"],))
        db.execute("CREATE TRIGGER failure BEFORE UPDATE OF stable ON release "
                   "BEGIN SELECT RAISE(ABORT,'interrupted'); END")
    with pytest.raises(sqlite3.IntegrityError, match="interrupted"):
        approve(root, change)
    assert len(history.snapshot(root)) == 1 and store.review(root, change["id"])["state"] == "passed"
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("DROP TRIGGER failure")
    approve(root, change)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TRIGGER failure BEFORE UPDATE OF stable ON release "
                   "BEGIN SELECT RAISE(ABORT,'interrupted'); END")
    with pytest.raises(sqlite3.IntegrityError, match="interrupted"):
        history.rollback(root, expected_seq=2, expected_sha=change["sha"], target_seq=1, target_sha=base)
    assert len(history.snapshot(root)) == 2 and store.release_status(root)["stable"] == change["sha"]


def test_publication_refuses_an_approved_candidate_without_a_version(release):
    root, base = release
    candidate = store.submit(root, proposal(base))
    with closing(sqlite3.connect(root / "release.sqlite3")) as db, db:
        db.execute("UPDATE candidates SET state='approved' WHERE id=?", (candidate["id"],))
    with pytest.raises(ValueError, match="Approval history"):
        history.snapshot(root)
