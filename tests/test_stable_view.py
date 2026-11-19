from contextlib import closing
import json
import sqlite3

import pytest

from agent_office import release_store, release_validation, release_view, stable_history, stable_view
from agent_office.models import OfficeError
from test_improvement_proposal import fixture, git
from test_release_store import result
from test_release_view import JOB, JOURNAL, ready, setup


def approve(root, artifact, monkeypatch):
    record = release_store.submit(root, artifact)
    monkeypatch.setattr(release_validation, "validate_candidate", lambda repo, sha, image: result(sha, image))
    release_store.verify_candidate(root, record["id"])
    return release_store.decide(root, record["id"], record["sha"], "approve")


def test_existing_view_opt_in_revocation_and_wrong_journal(setup, monkeypatch):
    journal, root, view, artifact = setup
    empty = {"available": False, "current": None, "updated_at": None, "items": [], "next_offset": None}
    release_view.sync_once(journal, root, view)
    assert stable_view.versions(view, JOURNAL) == stable_view.versions(None, JOURNAL) == empty
    with pytest.raises(OfficeError, match="NOT_FOUND"):
        stable_view.compare(view, JOURNAL, 1, 1)
    release_view.sync_once(journal, root, view, stable_history=True)
    page = stable_view.versions(view, JOURNAL)
    assert page["available"] and page["updated_at"] and page["current"] == page["items"][0]
    assert page["current"]["sha"] == artifact["base"] and page["current"]["kind"] == "initial"
    assert stable_view.versions(view, "d" * 32) == empty
    with pytest.raises(OfficeError, match="STORE_IO"):
        stable_view.compare(view, "d" * 32, 1, 1)
    with pytest.raises(OfficeError, match="NOT_FOUND"):
        stable_view.compare(view, JOURNAL, 1, 2)
    with sqlite3.connect(view) as target:
        target.execute("UPDATE publication SET stable=?", ("f" * 40,))
    for query in (lambda: stable_view.versions(view, JOURNAL), lambda: stable_view.compare(view, JOURNAL, 1, 1)):
        with pytest.raises(OfficeError, match="STORE_IO"):
            query()
    release_view.sync_once(journal, root, view, stable_history=True)
    assert stable_view.versions(view, JOURNAL)["current"] == page["current"]
    assert release_view.view_status(view, JOURNAL)["stable"] == artifact["base"]
    original = release_view._refresh

    def verify_revoked(*args):
        assert stable_view.versions(view, JOURNAL) == empty
        original(*args)

    monkeypatch.setattr(release_view, "_refresh", verify_revoked)
    release_view.sync_once(journal, root, view)
    assert release_view.view_status(view, JOURNAL)["stable"] == artifact["base"]


def test_approved_branches_net_diff_and_isolation(setup, monkeypatch):
    journal, root, view, artifact = setup
    base = artifact["base"]
    private_title = "private evidence from another journal"
    rejected = release_store.submit(root, dict(artifact, title=private_title, patch=artifact["patch"].replace(
        "test error", "rejected source secret")))
    release_store.decide(root, rejected["id"], rejected["sha"], "reject")
    new_file = "diff --git a/note b/note\nnew file mode 100644\n--- /dev/null\n+++ b/note\n@@ -0,0 +1 @@\n+승인\n"
    first = approve(root, dict(artifact, title=private_title, patch=artifact["patch"] + new_file), monkeypatch)
    stable_history.rollback(root, expected_seq=2, expected_sha=first["sha"], target_seq=1, target_sha=base)
    mode = "diff --git a/tool.py b/tool.py\nold mode 100644\nnew mode 100755\n"
    mode += artifact["patch"].split("\n", 1)[1].replace("test error", "second branch")
    second = approve(root, dict(artifact, title=private_title, patch=mode), monkeypatch)
    release_view.sync_once(journal, root, view, stable_history=True)
    # Simulate a Studio host with only the transferred publication.
    monkeypatch.setattr(release_store, "run_git", lambda *a, **kw: pytest.fail("Studio accessed Git"))
    monkeypatch.setattr(stable_history, "snapshot", lambda *a, **kw: pytest.fail("Studio accessed the release ledger"))
    rows = stable_view.versions(view, JOURNAL)["items"]
    assert [r["kind"] for r in rows] == ["promotion", "rollback", "promotion", "initial"]
    assert rows[0]["sha"] == second["sha"] and rows[1]["target_seq"] == 1
    assert release_view.view_candidates(view, JOURNAL)["items"] == []
    first_to_second = stable_view.compare(view, JOURNAL, 2, 4)
    assert first_to_second["files"] == [
        {"path": "note", "status": "deleted", "before_mode": "100644", "after_mode": None},
        {"path": "tool.py", "status": "modified", "before_mode": "100644", "after_mode": "100755"}]
    assert first_to_second["total_files"] == 2 and not first_to_second["truncated"]
    assert "-승인\n" in first_to_second["diff"] and "+print('second branch')\n" in first_to_second["diff"]
    reverse = stable_view.compare(view, JOURNAL, 4, 2)
    assert reverse["files"][0]["status"] == "added" and "+승인\n" in reverse["diff"]
    assert "old mode 100755\nnew mode 100644" in reverse["diff"]
    for start, end in ((1, 3), (3, 1), (4, 4)):
        diff = stable_view.compare(view, JOURNAL, start, end)
        assert (diff["diff"], diff["files"], diff["total_files"], diff["truncated"]) == ("", [], 0, False)
    with closing(sqlite3.connect(view)) as target:
        dump = "\n".join(target.iterdump())
    for secret in (private_title, rejected["id"], rejected["sha"], "rejected source secret",
                   first["id"], second["id"], artifact["rationale"]):
        assert secret not in dump
    release_view.sync_once(journal, root, view)
    with closing(sqlite3.connect(view)) as target:
        assert not target.execute("SELECT oid FROM stable_blobs").fetchall()
        assert not target.execute("SELECT path FROM stable_revisions").fetchall()


def test_atomic_update_and_idle_watch(setup, monkeypatch, tmp_path):
    journal, root, view, artifact = setup
    with closing(release_view.PublicationWatch()) as watch:
        sync = lambda: release_view.sync_once(journal, root, view, stable_history=True, watch=watch)
        sync()
        backup = tmp_path / "initial-view.sqlite3"
        with closing(sqlite3.connect(view)) as src, closing(sqlite3.connect(backup)) as dst:
            src.backup(dst)
        first = approve(root, artifact, monkeypatch)
        ready(journal, artifact)
        with monkeypatch.context() as m:
            def fail(db, git_dir, event):
                assert stable_view.versions(view, JOURNAL)["current"]["seq"] == 1
                assert release_view.view_status(view, JOURNAL)["stable"] == artifact["base"]
                raise ValueError("interrupted projection")
            m.setattr(stable_view, "_index_transition", fail)
            with pytest.raises(ValueError, match="interrupted projection"):
                sync()
        sync()
        assert release_view.view_status(view, JOURNAL)["stable"] == first["sha"]
        assert stable_view.versions(view, JOURNAL)["current"]["seq"] == 2
        with monkeypatch.context() as m:
            m.setattr(stable_history, "snapshot", lambda *a: pytest.fail("idle poll scanned the ledger"))
            m.setattr(stable_view, "_index_transition", lambda *a: pytest.fail("idle poll rescanned Git"))
            sync()
        # A different publisher can revoke history between polls of this watcher.
        release_view.sync_once(journal, root, view)
        assert not stable_view.versions(view, JOURNAL)["available"]
        sync()
        assert stable_view.versions(view, JOURNAL)["current"]["seq"] == 2
        # Legacy Studio kept this link only in its publication. Upgrading the same
        # view verifies the receipt and backfills a durable release-store binding.
        with release_store._db(root, write=True) as source:
            source.execute("DROP TABLE studio_imports")
        sync()
        with release_store._db(root) as source:
            assert source.execute("SELECT candidate FROM studio_imports WHERE journal=? AND job=?",
                                  (JOURNAL, JOB)).fetchone()[0] == first["id"]
        stable_history.rollback(root, expected_seq=2, expected_sha=first["sha"], target_seq=1, target_sha=artifact["base"])
        sync()
        assert stable_view.versions(view, JOURNAL)["current"]["seq"] == 3
        assert release_view.view_job(view, JOURNAL, JOB)["candidate_id"] == first["id"]
        assert release_view.view_candidate(view, JOURNAL, first["id"])["job_id"] == JOB
        fresh = tmp_path / "fresh-view.sqlite3"
        release_view.sync_once(journal, root, fresh, stable_history=True)
        assert release_view.view_job(fresh, JOURNAL, JOB)["candidate_id"] == first["id"]
        assert len(release_store.list_candidates(root)) == 1
        new_job = "f" * 32
        ready(journal, artifact, new_job)
        sync()
        next_id = release_view.view_job(view, JOURNAL, new_job)["candidate_id"]
        assert next_id and next_id != first["id"]
        assert release_view.view_candidate(view, JOURNAL, next_id)["validation"] == "passed"
        # In-place SQLite restore retains the inode and the stable SHA, but replaces
        # the rollback event (#3) with its original occurrence (#1).
        inode = view.stat().st_ino
        with closing(sqlite3.connect(backup)) as src, closing(sqlite3.connect(view)) as dst:
            src.backup(dst)
        assert view.stat().st_ino == inode and stable_view.versions(view, JOURNAL)["current"]["seq"] == 1
        sync()
        assert stable_view.versions(view, JOURNAL)["current"]["seq"] == 3
        assert release_view.view_job(view, JOURNAL, JOB)["candidate_id"] == first["id"]
        assert release_view.view_job(view, JOURNAL, new_job)["candidate_id"] == next_id
        assert len(release_store.list_candidates(root)) == 2


def test_diff_bounds_and_no_unchanged_blobs(setup, fixture, monkeypatch):
    journal, root, view, artifact = setup
    _, repo, _ = fixture
    # Use two synthetic stable trees to exercise binary preview handling independently
    # of the production admission gate that currently accepts UTF-8 patches only.
    git(repo, "config", "core.autocrlf", "false")
    (repo / "unchanged").write_text("unchanged sensitive source", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "benchmark base")
    base = git(repo, "rev-parse", "HEAD").decode()
    (repo / "00-binary").write_bytes(b"x\x00hidden")
    (repo / "01-large").write_bytes(b"x" * (stable_view.MAX_BLOB + 1))
    (repo / "02-text").write_bytes(("한" * 21500 + "\n").encode())
    (repo / "03-text").write_bytes(("글" * 1000 + "\n").encode())
    for i in range(100):
        (repo / f"extra-{i:03}").write_text("one copy", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "update-index", "--chmod=+x", "tool.py")
    git(repo, "commit", "-qm", "benchmark next")
    sha = git(repo, "rev-parse", "HEAD").decode()
    git(root / "objects.git", "fetch", str(repo), sha)
    events = [dict(seq=1, kind="initial", previous=None, sha=base, time=None, target_seq=None),
              dict(seq=2, kind="promotion", previous=base, sha=sha, time="2026-11-19T12:00:00+00:00", target_seq=None)]
    monkeypatch.setattr(stable_history, "snapshot", lambda _, **kw: events)
    release_view.sync_once(journal, root, view, stable_history=True)
    compared = stable_view.compare(view, JOURNAL, 1, 2)
    assert compared["total_files"] == 105 and len(compared["files"]) == 100
    assert compared["truncated"] and len(compared["diff"].encode()) <= 65536
    assert "binary or non-UTF-8" in compared["diff"] and "oversized" in compared["diff"]
    assert "+한" in compared["diff"] and compared["diff"].endswith("[truncated]")
    with closing(sqlite3.connect(view)) as target:
        assert target.execute("SELECT count(*) FROM stable_blobs").fetchone() == (5,)
        assert not target.execute("SELECT 1 FROM stable_revisions WHERE path='unchanged'").fetchone()
        assert "unchanged sensitive source" not in "\n".join(target.iterdump())


def test_page_offset_and_restored_release(setup, monkeypatch, tmp_path):
    journal, root, view, artifact = setup
    release_view.sync_once(journal, root, view, stable_history=True)
    initial, promoted = artifact["base"], approve(root, artifact, monkeypatch)["sha"]
    database = root / "release.sqlite3"
    backup = tmp_path / "approved-backup.sqlite3"
    with closing(sqlite3.connect(database)) as src, closing(sqlite3.connect(backup)) as dst:
        src.backup(dst)
    for seq in range(2, 26):
        if seq % 2 == 0:
            stable_history.rollback(root, expected_seq=seq, expected_sha=promoted, target_seq=1, target_sha=initial)
        else:
            stable_history.rollback(root, expected_seq=seq, expected_sha=initial, target_seq=2, target_sha=promoted)
    release_view.sync_once(journal, root, view, stable_history=True)
    first = stable_view.versions(view, JOURNAL)
    second = stable_view.versions(view, JOURNAL, first["next_offset"])
    assert [r["seq"] for r in first["items"] + second["items"]] == list(range(26, 0, -1))
    assert first["next_offset"] == 24 and second["next_offset"] is None
    assert first["current"] == second["current"] == first["items"][0]
    with closing(sqlite3.connect(backup)) as src, closing(sqlite3.connect(database)) as dst:
        src.backup(dst)
    release_view.sync_once(journal, root, view, stable_history=True)
    assert [r["seq"] for r in stable_view.versions(view, JOURNAL)["items"]] == [2, 1]
    with pytest.raises(OfficeError, match="NOT_FOUND"):
        stable_view.compare(view, JOURNAL, 1, 26)


def test_line_comparison_budget_is_shared_and_allows_additions(setup, monkeypatch):
    journal, root, view, artifact = setup
    def add(path, text):
        return (f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
                f"@@ -0,0 +1,2 @@\n+first\n+{text}\n")
    def edit(path):
        return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
                "@@ -1,2 +1,2 @@\n first\n-before\n+after\n")
    first = approve(root, dict(artifact, patch=add("a", "before") + add("b", "before")), monkeypatch)
    approve(root, dict(artifact, base=first["sha"], patch=edit("a") + edit("b") + add("c", "new")), monkeypatch)
    release_view.sync_once(journal, root, view, stable_history=True)
    monkeypatch.setattr(stable_view, "MAX_LINE_PAIRS", 4)
    compared = stable_view.compare(view, JOURNAL, 2, 3)
    assert compared["total_files"] == 3 and compared["truncated"]
    assert [r["status"] for r in compared["files"]] == ["modified", "modified", "added"]
    assert compared["diff"].count("-before\n+after\n") == 1
    assert "diff --git a/b b/b\n[content unavailable: line comparison budget exceeded]" in compared["diff"]
    assert "+new\n" in compared["diff"]


def test_cached_candidate_detects_stale_base_after_round_trip(setup, monkeypatch):
    journal, root, view, artifact = setup
    candidate = release_store.submit(root, artifact)
    monkeypatch.setattr(release_validation, "validate_candidate", lambda repo, sha, image: result(sha, image))
    release_store.verify_candidate(root, candidate["id"])
    release_view.sync_once(journal, root, view)
    fresh = release_view.view_candidate(view, JOURNAL, candidate["id"])
    assert fresh["validation"] == "passed" and fresh["stale_base"] is False
    # Old publications have no version-relative staleness field. Keep them readable
    # and populate the optional field on the next (even non-history) publication.
    with closing(sqlite3.connect(view)) as target, target:
        old = json.loads(target.execute("SELECT summary FROM candidates WHERE id=?", (candidate["id"],)).fetchone()[0])
        del old["stale_base"]
        target.execute("UPDATE candidates SET summary=? WHERE id=?", (json.dumps(old), candidate["id"]))
    assert "stale_base" not in release_view.view_candidate(view, JOURNAL, candidate["id"])
    release_view.sync_once(journal, root, view)
    assert release_view.view_candidates(view, JOURNAL)["items"][0]["stale_base"] is False
    # No intermediate sync occurs. The stable SHA is identical at both publications.
    promoted = approve(root, dict(artifact, title="Private alternative", patch=artifact["patch"].replace(
        "test error", "alternate")), monkeypatch)
    stable_history.rollback(root, expected_seq=2, expected_sha=promoted["sha"], target_seq=1, target_sha=artifact["base"])
    release_view.sync_once(journal, root, view)
    stale = release_view.view_candidate(view, JOURNAL, candidate["id"])
    assert stale["state"] == stale["validation"] == "passed"
    assert stale["base"] == release_view.view_status(view, JOURNAL)["stable"] and stale["stale_base"] is True
    assert release_view.view_candidates(view, JOURNAL)["items"][0]["stale_base"] is True


def test_replaced_release_cannot_publish_under_other_identity(setup, fixture, monkeypatch, tmp_path):
    journal, root, view, artifact = setup
    _, repo, base = fixture
    release_view.sync_once(journal, root, view, stable_history=True)
    before = stable_view.versions(view, JOURNAL)
    replacement = tmp_path / "another-release"
    release_store.init_release(replacement, repo, base, release_store.release_status(root)["image"])
    snapshot = stable_history.snapshot

    def replace_then_snapshot(selected, **kwargs):
        with closing(sqlite3.connect(replacement / "release.sqlite3")) as src:
            with closing(sqlite3.connect(selected / "release.sqlite3")) as dst:
                src.backup(dst)
        return snapshot(selected, **kwargs)

    monkeypatch.setattr(stable_history, "snapshot", replace_then_snapshot)
    with pytest.raises(ValueError, match="Release identity changed"):
        release_view.sync_once(journal, root, view, stable_history=True)
    assert stable_view.versions(view, JOURNAL) == before
