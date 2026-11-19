import json
import sqlite3

import pytest

from agent_office import cli, improvement_proposal as proposals, release_store, release_validation
from agent_office import release_view as publication, studio_improvements as jobs
from agent_office.improvement_wire import check_improvement_reply
from agent_office.models import OfficeError
from agent_office.store import TaskStore, encode
from test_improvement_proposal import fixture, answer, RUN
from test_release_store import IMAGE, result

JOURNAL = "a" * 32
JOB = "b" * 32


@pytest.fixture
def setup(fixture, tmp_path):
    journal, repo, base = fixture
    root, view = tmp_path / "release", tmp_path / "published" / "view.sqlite3"
    with TaskStore(journal, existing=True) as store:
        store.connection.execute("CREATE TABLE studio_identity(singleton INTEGER PRIMARY KEY, id TEXT)")
        store.connection.execute("INSERT INTO studio_identity VALUES(1,?)", (JOURNAL,))
        store.connection.execute(jobs._SCHEMA)
    release_store.init_release(root, repo, base, IMAGE)
    history = proposals._history(journal, [RUN])
    response = answer(history)
    proposals._bind_citations(response, history)
    response.pop("citations")
    artifact = dict(version=1, engine="codex", base=base, history=history, **response)
    proposals._record_generation(journal, artifact)
    return journal, root, view, artifact


def ready(journal, artifact, id=JOB, digest=None):
    with TaskStore(journal, existing=True) as store:
        store.connection.execute("""INSERT INTO studio_improvements(id,base,engine,run_ids,state,title,artifact,digest)
                                    VALUES(?,?,?,?,'ready',?,?,?)""",
                                 (id, artifact["base"], artifact["engine"], encode([RUN]), artifact["title"],
                                  encode(artifact), digest or proposals._digest(artifact)))


def test_publication_observes_running_validation_and_operator_decisions(setup, monkeypatch, capsys):
    db, root, view, artifact = setup
    command = ["--db", str(db), "improve", "--release", str(root), "studio-sync", "--view", str(view)]
    assert cli.main(command) == 0
    assert json.loads(capsys.readouterr().out)["candidates"] == 0
    assert publication.view_status(view, JOURNAL)["stable"] == artifact["base"]
    ready(db, artifact)
    calls = []

    def validate(git_dir, sha, image):
        row, = publication.view_candidates(view, JOURNAL)["items"]
        assert image == IMAGE and sha == row["sha"]
        assert row["validation"] == "running"
        calls.append(sha)
        return result(sha, image, False)

    monkeypatch.setattr(release_validation, "validate_candidate", validate)
    assert publication.sync_once(db, root, view)["candidates"] == 1
    link = publication.view_job(view, JOURNAL, JOB)
    cid = link["candidate_id"]
    detail = publication.view_candidate(view, JOURNAL, cid)
    assert link["error"] is None and detail["state"] == "failed"
    assert detail["exit_code"] == 1 and detail["run_ids"] == [RUN] and "+print('test error')" in detail["diff"]
    assert detail["validation_output"] == "test output" and detail["job_id"] == JOB
    check_improvement_reply(200, {"candidate": detail, "studio_run_ids": []}, ("candidate", cid), None)
    publication.sync_once(db, root, view)
    assert calls == [detail["sha"]]
    monkeypatch.setattr(release_validation, "validate_candidate", lambda repo, sha, image: result(sha, image))
    release_store.verify_candidate(root, cid)
    release_store.decide(root, cid, detail["sha"], "approve")
    publication.sync_once(db, root, view)
    assert publication.view_status(view, JOURNAL)["stable"] == detail["sha"]
    latest = publication.view_candidate(view, JOURNAL, cid)
    assert latest["state"] == latest["decision"] == "approved" and latest["validation"] == "passed"
    assert latest["history"][-1]["event"] == "approved"
    check_improvement_reply(200, {"candidate": latest, "studio_run_ids": []}, ("candidate", cid), None)


def test_cli_provenance_and_refused_studio_imports(setup, monkeypatch):
    db, root, view, artifact = setup
    monkeypatch.setattr(release_validation, "validate_candidate", lambda *_: pytest.fail("not a Studio validation"))
    other = dict(artifact, title="Another proposal")
    hidden = release_store.submit(root, other)
    authentic = release_store.submit(root, artifact)
    ready(db, other, digest="invalid")
    stale = dict(artifact, base="0" * 40)
    proposals._record_generation(db, stale)
    ready(db, stale, "c" * 32)
    summary = publication.sync_once(db, root, view)
    assert summary["job_errors"] == 2 and summary["candidates"] == 1
    assert publication.view_job(view, JOURNAL, JOB)["error"]
    assert "base differs" in publication.view_job(view, JOURNAL, "c" * 32)["error"]
    seen, = publication.view_candidates(view, JOURNAL)["items"]
    assert seen["id"] == authentic["id"] and seen["job_id"] is None and seen["validation"] is None
    with pytest.raises(OfficeError, match="NOT_FOUND"):
        publication.view_candidate(view, JOURNAL, hidden["id"])
    # Terminal import errors are durable across restart.
    monkeypatch.setattr(jobs, "ready_artifact", lambda *args: pytest.fail("repeat import"))
    publication.sync_once(db, root, view)


def test_interruption_keeps_receipt_and_requires_manual_validation(setup, monkeypatch):
    db, root, view, artifact = setup
    ready(db, artifact)
    def interrupt(*args):
        raise KeyboardInterrupt
    monkeypatch.setattr(release_validation, "validate_candidate", interrupt)
    with pytest.raises(KeyboardInterrupt):
        publication.sync_once(db, root, view)
    publication.sync_once(db, root, view)
    cid = publication.view_job(view, JOURNAL, JOB)["candidate_id"]
    record = publication.view_candidate(view, JOURNAL, cid)
    assert record["state"] == "submitted" and record["validation"] is None
    assert "interrupted" in record["validation_output"]
    assert len(release_store.list_candidates(root)) == 1


def test_publication_identity_and_destination_boundaries(setup, fixture, tmp_path):
    db, root, view, artifact = setup
    base = artifact["base"]
    _, repo, _ = fixture
    unrelated = tmp_path / "unrelated.db"
    with sqlite3.connect(unrelated) as conn:
        conn.execute("CREATE TABLE retained(data TEXT)")
    for forbidden in (unrelated, db, db.with_name(db.name + "-wal"), root / "release.sqlite3", repo / "view"):
        with pytest.raises(ValueError):
            publication.sync_once(db, root, forbidden)
    with sqlite3.connect(unrelated) as conn:
        assert conn.execute("SELECT name FROM sqlite_master").fetchall() == [("retained",)]
    assert not (repo / "view").exists()
    publication.sync_once(db, root, view)
    wrong = "d" * 32
    with pytest.raises(OfficeError, match="STORE_IO"):
        publication.view_candidates(view, wrong)
    assert publication.view_status(view, wrong)["available"] is False
    with pytest.raises(OfficeError, match="STORE_IO"):
        publication.view_job(view, wrong, JOB)
    another = tmp_path / "other release"
    release_store.init_release(another, repo, base, IMAGE)
    with pytest.raises(ValueError, match="another release"):
        publication.sync_once(db, another, view)
    with TaskStore(db, existing=True) as store:
        store.connection.execute("UPDATE studio_identity SET id=?", (wrong,))
    with pytest.raises(ValueError, match="another release or journal"):
        publication.sync_once(db, root, view)
    missing = tmp_path / "no-view"
    assert publication.view_status(missing, JOURNAL)["available"] is False and not missing.exists()

def test_concurrent_publishers_and_crash_after_submission(setup, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from agent_office.ownership import RunBusyError
    db, root, view, artifact = setup
    ready(db, artifact)
    real_submit = release_store.submit
    def crash_after_submit(*args):
        real_submit(*args)
        raise KeyboardInterrupt
    with monkeypatch.context() as sub:
        sub.setattr(release_store, "submit", crash_after_submit)
        with pytest.raises(KeyboardInterrupt):
            publication.sync_once(db, root, view)
    validating, resume = Event(), Event()
    calls = []
    def validate(repo, sha, image):
        calls.append(sha)
        validating.set()
        assert resume.wait(timeout=10)
        return result(sha, image)
    monkeypatch.setattr(release_validation, "validate_candidate", validate)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(publication.sync_once, db, root, view)
        try:
            assert validating.wait(timeout=10)
            assert publication.view_candidates(view, JOURNAL)["items"][0]["validation"] == "running"
            with pytest.raises(RunBusyError):
                publication.sync_once(db, root, view)
        finally:
            resume.set()
        assert future.result()["candidates"] == 1
    assert len(calls) == 1 and len(release_store.list_candidates(root)) == 1
    publication.sync_once(db, root, view)
    assert len(calls) == 1


def test_transient_import_can_resume_without_busy_retry(setup, monkeypatch):
    db, root, view, artifact = setup
    ready(db, artifact)
    clock = [100]
    monkeypatch.setattr(publication.time, "time", lambda: clock[0])
    original = release_store.submit
    calls = []
    def flaky(root, proposal):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("temporary failure with private operator path")
        return original(root, proposal)
    monkeypatch.setattr(release_store, "submit", flaky)
    monkeypatch.setattr(release_validation, "validate_candidate", lambda repo, sha, image: result(sha, image))
    for i in range(2):
        assert publication.sync_once(db, root, view)["job_errors"] == 1
    assert len(calls) == 1 and "private" not in publication.view_job(view, JOURNAL, JOB)["error"]
    clock[0] += 31
    assert publication.sync_once(db, root, view)["job_errors"] == 0
    assert len(calls) == 2 and publication.view_job(view, JOURNAL, JOB)["candidate_id"]


def test_publication_retains_utf8_limits_and_latest_history(setup, monkeypatch):
    db, root, view, artifact = setup
    candidate = release_store.submit(root, artifact)
    cid = candidate["id"]
    with release_store._db(root, write=True) as source:
        for _ in range(40):
            release_store._log(source, cid, "validation_started", {})
        release_store._log(source, cid, "validation_finished",
                           result(candidate["sha"], IMAGE) | {"output": "한" * 10000})
        source.execute("UPDATE candidates SET state='passed' WHERE id=?", (cid,))
    monkeypatch.setattr(release_store, "_diff", lambda *args: "한" * 20000)
    publication.sync_once(db, root, view)
    detail = publication.view_candidate(view, JOURNAL, cid)
    assert detail["diff_truncated"] and detail["output_truncated"]
    assert len(detail["diff"].encode()) <= 48 * 1024 and detail["diff"].endswith("[truncated]")
    assert len(detail["validation_output"].encode()) <= 16 * 1024
    assert len(detail["history"]) == 32 and detail["history_omitted"] == 10
    check_improvement_reply(200, {"candidate": detail, "studio_run_ids": []}, ("candidate", cid), None)

def test_idle_watch_skips_provenance_and_recovers_abandoned_operator_retry(setup, monkeypatch):
    db, root, view, artifact = setup
    ready(db, artifact)
    watch = publication.PublicationWatch()
    monkeypatch.setattr(release_validation, "validate_candidate", lambda repo, sha, image: result(sha, image, False))
    try:
        for _ in range(3):
            publication.sync_once(db, root, view, watch=watch)
        cid = publication.view_job(view, JOURNAL, JOB)["candidate_id"]
        with monkeypatch.context() as m:
            m.setattr(publication, "verify_history", lambda *_: pytest.fail("idle watch reverified history"))
            m.setattr(publication, "_projection", lambda *_: pytest.fail("idle watch regenerated projection"))
            publication.sync_once(db, root, view, watch=watch)
        with release_store._guard(root, cid):
            with release_store._db(root, write=True) as source:
                release_store._log(source, cid, "validation_started", {})
            publication.sync_once(db, root, view, watch=watch)
            row = publication.view_candidate(view, JOURNAL, cid)
            assert row["state"] == "failed" and row["validation"] == "running"
        publication.sync_once(db, root, view, watch=watch)
        row = publication.view_candidate(view, JOURNAL, cid)
        assert row["validation"] is None and "interrupted" in row["validation_output"]
        # Restoring a release backup can remove a referenced candidate.
        with release_store._db(root, write=True) as source:
            source.execute("DELETE FROM history WHERE candidate=?", (cid,))
            source.execute("DELETE FROM candidates WHERE id=?", (cid,))
        publication.sync_once(db, root, view, watch=watch)
        assert publication.view_candidates(view, JOURNAL)["items"] == []
        assert publication.view_job(view, JOURNAL, JOB)["candidate_id"] is None
        assert "missing" in publication.view_job(view, JOURNAL, JOB)["error"]
    finally:
        watch.close()


def test_replaced_source_cannot_publish_another_hosts_jobs_under_first_identity(setup, tmp_path, monkeypatch):
    from contextlib import closing
    db, root, view, artifact = setup
    ready(db, artifact)
    second = tmp_path / "other-host.sqlite3"
    with closing(sqlite3.connect(db)) as one, closing(sqlite3.connect(second)) as two:
        one.backup(two)
        with two:
            two.execute("UPDATE studio_identity SET id=?", ("9" * 32,))
            two.execute("DELETE FROM studio_improvements")
            two.execute("DELETE FROM improvement_receipts")
    foreign = dict(artifact, title="Another host generated this", patch=artifact["patch"].replace("test error", "different"))
    proposals._record_generation(second, foreign)
    ready(second, foreign, "c" * 32)
    original_import = publication._import_jobs
    def replace_then_import(release_root, pinned, target):
        with closing(sqlite3.connect(second)) as src, closing(sqlite3.connect(db)) as dest:
            src.backup(dest)
        original_import(release_root, pinned, target)
    monkeypatch.setattr(publication, "_import_jobs", replace_then_import)
    monkeypatch.setattr(release_validation, "validate_candidate", lambda repo, sha, image: result(sha, image))
    publication.sync_once(db, root, view)
    row, = publication.view_candidates(view, JOURNAL)["items"]
    assert row["title"] == artifact["title"] and row["job_id"] == JOB
    assert len(release_store.list_candidates(root)) == 1
    with pytest.raises(ValueError, match="another release or journal"):
        publication.sync_once(db, root, view)


def test_configured_view_io_errors_do_not_masquerade_as_empty(setup, monkeypatch):
    db, root, view, _ = setup
    publication.sync_once(db, root, view)
    def locked(path, mode):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(publication, "_open", locked)
    for query in (publication.view_status, publication.view_candidates):
        with pytest.raises(OfficeError, match="STORE_IO"):
            query(view, JOURNAL)

def test_restored_job_digest_rebinds_candidate_and_paged_views_are_bounded(setup, monkeypatch):
    db, root, view, artifact = setup
    ready(db, artifact)
    monkeypatch.setattr(release_validation, "validate_candidate", lambda repo, sha, image: result(sha, image))
    publication.sync_once(db, root, view)
    first = publication.view_job(view, JOURNAL, JOB)["candidate_id"]
    replacement = dict(artifact, title="Another generated improvement")
    proposals._record_generation(db, replacement)
    with TaskStore(db, existing=True) as store:
        store.connection.execute("DELETE FROM studio_improvements")
    ready(db, replacement)
    publication.sync_once(db, root, view)
    second = publication.view_job(view, JOURNAL, JOB)["candidate_id"]
    assert second != first and publication.view_candidate(view, JOURNAL, first)["job_id"] is None
    assert publication.view_candidate(view, JOURNAL, second)["title"] == replacement["title"]
    # Exercise paging with an existing projection without invoking a validator 24 more times.
    with sqlite3.connect(view) as target:
        for i in range(24):
            cid = f"{i:032x}"
            summary = dict(publication.view_candidates(view, JOURNAL)["items"][0], id=cid)
            target.execute("INSERT INTO candidates VALUES(?,?,?,0,?,?)",
                           (cid, "fixture", "fixture", encode(summary), "{}"))
    page1 = publication.view_candidates(view, JOURNAL, 0)
    page2 = publication.view_candidates(view, JOURNAL, page1["next_offset"])
    assert len(page1["items"]) == 24 and len(page2["items"]) == 2 and page2["next_offset"] is None
    assert {row["id"] for row in page2["items"]} == {first, second}
