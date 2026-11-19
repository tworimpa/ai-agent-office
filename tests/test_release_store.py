from concurrent.futures import ThreadPoolExecutor
import threading

import pytest

from agent_office import release_store as store
from agent_office import release_validation


IMAGE = "sha256:" + "e" * 64


def proposal(base, before="old", after="new", path="example.txt"):
    return {
        "version": 1, "engine": "codex", "base": base, "title": "Show detailed results",
        "expected": "The output identifies the failing test.", "rationale": "The selected run failed.",
        "history": [{"run_id": "a" * 32, "request": "Improve test reporting",
                     "state": "FAILED", "outcome": "TEST_FAILED", "citation": 1,
                     "events": [{"seq": 1, "type": "RUN_FINISHED", "payload": {
                         "phase": "run", "attempt": 1, "state": "FAILED", "outcome": "TEST_FAILED"
                     }}]}],
        "patch": (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n"
                  f"@@ -1 +1 @@\n-{before}\n+{after}\n"),
    }


@pytest.fixture
def source(tmp_path):
    source = tmp_path / "source 한글"
    source.mkdir()
    store.run_git(source, "init", "--template=")
    (source / "example.txt").write_bytes(b"old\n")
    store.run_git(source, "add", "--", "example.txt")
    store.run_git(source, "commit", "-m", "Initial source")
    return source


@pytest.fixture
def release(source, tmp_path):
    root = tmp_path / "release"
    initial = store.init_release(root, source, "HEAD", IMAGE)
    return root, initial["stable"]


def result(sha, image, passed=True):
    return dict(sha=sha, image=image, passed=passed, exit_code=0 if passed else 1,
                output="test output")


def test_source_promotion_exports_independent_checkout(release, source, tmp_path, monkeypatch):
    root, base = release
    change = proposal(base)
    monkeypatch.setenv("GIT_DIR", str(source / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(source / "unexpected-index"))
    candidate = store.submit(root, change)
    id, sha = candidate["id"], candidate["sha"]
    assert store.submit(root, change) == candidate
    other = store.submit(root, proposal(base, after="alternative"))
    review = store.review(root, id)
    assert review["proposal"] == change and review["stable"] == base
    assert "+new\n" in review["diff"] and "-old\n" in review["diff"]
    assert store.run_git(root / "objects.git", "rev-parse", sha + "^").strip().decode() == base
    assert not (source / "unexpected-index").exists()
    with pytest.raises(ValueError, match="passed"):
        store.decide(root, id, sha, "approve")
    with pytest.raises(ValueError, match="SHA"):
        store.decide(root, id, base, "reject")
    calls = []

    def validate(repo, candidate_sha, image):
        assert repo == root / "objects.git" and image == IMAGE
        calls.append(candidate_sha)
        return result(candidate_sha, image)

    monkeypatch.setattr(release_validation, "validate_candidate", validate)
    assert store.verify_candidate(root, id)["state"] == "passed"
    assert store.verify_candidate(root, id)["validation"] == result(sha, IMAGE)
    assert calls == [sha]
    approved = store.decide(root, id, sha, "approve")
    assert approved == store.decide(root, id, sha, "approve")
    assert [e["event"] for e in store.review(root, id)["history"]] == [
        "submitted", "validation_started", "validation_finished", "approved"]
    with pytest.raises(ValueError, match="different final"):
        store.decide(root, id, sha, "reject")
    dest = tmp_path / "next source"
    assert store.checkout_stable(root, dest)["stable"] == sha
    assert (dest / "example.txt").read_bytes() == b"new\n"
    assert (source / "example.txt").read_bytes() == b"old\n"
    assert store.run_git(dest, "rev-parse", "HEAD").strip().decode() == sha
    assert store.run_git(dest, "remote") == b"" and store.run_git(dest, "status", "--porcelain") == b""
    assert not (dest / ".git/objects/info/alternates").exists()
    with pytest.raises(ValueError):
        store.run_git(dest, "cat-file", "-e", other["sha"])
    with pytest.raises(FileExistsError):
        store.checkout_stable(root, dest)
    with pytest.raises(ValueError, match="stale"):
        store.verify_candidate(root, other["id"])
    assert store.submit(root, proposal(sha, before="new", after="newer"))["base"] == sha
    with pytest.raises(ValueError, match="base differs"):
        store.submit(root, proposal(base, after="changed again"))


def test_failed_identity_or_interrupted_validation_can_retry(release, monkeypatch):
    root, base = release
    candidate = store.submit(root, proposal(base))
    id, sha = candidate["id"], candidate["sha"]

    def interrupted(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(release_validation, "validate_candidate", interrupted)
    with pytest.raises(KeyboardInterrupt):
        store.verify_candidate(root, id)
    assert store.review(root, id)["history"][-1]["event"] == "validation_started"
    with pytest.raises(ValueError, match="passed"):
        store.decide(root, id, sha, "approve")
    monkeypatch.setattr(release_validation, "validate_candidate", lambda *_: result(base, IMAGE))
    assert store.verify_candidate(root, id)["state"] == "failed"
    monkeypatch.setattr(release_validation, "validate_candidate", lambda *_: result(sha, IMAGE, False))
    assert store.verify_candidate(root, id)["validation"]["exit_code"] == 1
    with pytest.raises(ValueError, match="passed"):
        store.decide(root, id, sha, "approve")
    monkeypatch.setattr(release_validation, "validate_candidate", lambda *_: result(sha, IMAGE))
    assert store.verify_candidate(root, id)["state"] == "passed"
    rejected = store.decide(root, id, sha, "reject")
    assert rejected["state"] == "rejected" and store.decide(root, id, sha, "reject") == rejected
    assert store.review(root, id)["stable"] == base
    with pytest.raises(ValueError):
        store.verify_candidate(root, id)
    with pytest.raises(ValueError):
        store.decide(root, id, sha, "approve")


def test_active_validation_and_concurrent_approvals(release, monkeypatch):
    root, base = release
    first = store.submit(root, proposal(base))
    second = store.submit(root, proposal(base, after="alternative"))
    started, finish = threading.Event(), threading.Event()

    def validate(repo, sha, image):
        started.set()
        assert finish.wait(timeout=20)
        return result(sha, image)

    monkeypatch.setattr(release_validation, "validate_candidate", validate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(store.verify_candidate, root, first["id"])
        try:
            assert started.wait(timeout=20)
            with pytest.raises(ValueError, match="active"):
                store.verify_candidate(root, first["id"])
            with pytest.raises(ValueError, match="active"):
                store.decide(root, first["id"], first["sha"], "reject")
        finally:
            finish.set()
        assert pending.result()["state"] == "passed"
        assert store.verify_candidate(root, second["id"])["state"] == "passed"
        futures = [pool.submit(store.decide, root, c["id"], c["sha"], "approve") for c in (first, second)]
    assert sum(f.exception() is None for f in futures) == 1
    assert sorted(c["state"] for c in store.list_candidates(root)) == ["approved", "passed"]
    assert "base differs" in str(next(f.exception() for f in futures if f.exception()))


def test_reject_invalid_and_unsafe_patches(release, tmp_path):
    root, base = release
    invalid = [
        proposal(base, before="missing context"), proposal(base, after="old"),
        proposal(base, path="../outside"), proposal(base, path=".git/config"),
        proposal(base) | {"sha": base},
    ]
    link = proposal(base)
    link["patch"] = ("diff --git a/link b/link\nnew file mode 120000\n--- /dev/null\n"
                     "+++ b/link\n@@ -0,0 +1 @@\n+../outside\n")
    for change in [*invalid, link]:
        with pytest.raises(ValueError):
            store.submit(root, change)
    assert store.list_candidates(root) == [] and not (tmp_path / "outside").exists()


def test_initialization_boundaries_and_independent_objects(source, tmp_path):
    root = tmp_path / "release"
    with pytest.raises(ValueError, match="outside"):
        store.init_release(source / "release", source, "HEAD", IMAGE)
    linked = tmp_path / "linked tree"
    store.run_git(source, "worktree", "add", "--detach", str(linked))
    with pytest.raises(ValueError, match="outside"):
        store.init_release(source / "release", linked, "HEAD", IMAGE)
    with pytest.raises(ValueError, match="pinned"):
        store.init_release(root, source, "HEAD", "validator:latest")
    root.mkdir()
    with pytest.raises(FileExistsError):
        store.init_release(root, source, "HEAD", IMAGE)
    root = tmp_path / "independent"
    base = store.init_release(root, source, "HEAD", IMAGE)["stable"]
    source.rename(tmp_path / "moved source")
    with pytest.raises(ValueError, match="outside"):
        store.checkout_stable(root, root / "output")
    assert store.checkout_stable(root, tmp_path / "export")["stable"] == base
    assert not (root / "objects.git/objects/info/alternates").exists()


def test_operator_image_is_pinned_per_candidate(release, monkeypatch):
    root, base = release
    change = proposal(base)
    default = store.submit(root, change)
    custom_image = "sha256:" + "c" * 64
    custom = store.submit(root, change, image=custom_image)
    assert custom == store.submit(root, change, image=custom_image)
    assert custom["id"] != default["id"] and custom["sha"] != default["sha"]
    assert store.review(root, custom["id"])["image"] == custom_image
    with pytest.raises(ValueError, match="pinned"):
        store.submit(root, change, image="latest")
    monkeypatch.setattr(release_validation, "validate_candidate", lambda repo, sha, image: result(sha, image))
    assert store.verify_candidate(root, custom["id"])["validation"]["image"] == custom_image


def test_attributes_do_not_hide_diff_or_transform_export(source, tmp_path):
    (source / ".gitattributes").write_bytes(b"example.txt -diff working-tree-encoding=UTF-16\n")
    store.run_git(source, "add", ".gitattributes")
    store.run_git(source, "commit", "-m", "Track attributes")
    root = tmp_path / "release"
    base = store.init_release(root, source, "HEAD", IMAGE)["stable"]
    candidate = store.submit(root, proposal(base))
    diff = store.review(root, candidate["id"])["diff"]
    assert "-old\n" in diff and "+new\n" in diff
    dest = tmp_path / "export"
    store.checkout_stable(root, dest)
    assert (dest / "example.txt").read_bytes() == b"old\n"
    assert store.run_git(dest, "status", "--porcelain") == b""


def test_failed_publication_is_retryable_without_overwriting_another_creator(source, tmp_path, monkeypatch):
    root = tmp_path / "release"

    def disk_error(*args):
        raise OSError("disk error")

    with monkeypatch.context() as m:
        m.setattr(release_validation, "committed_files", disk_error)
        with pytest.raises(OSError, match="disk error"):
            store.init_release(root, source, "HEAD", IMAGE)
    assert not root.exists()
    initial = store.init_release(root, source, "HEAD", IMAGE)
    assert initial == store.release_status(root)
    dest = tmp_path / "output"
    export = store._export_checkout

    def interrupted(stage, *args):
        (stage / "partial").write_bytes(b"unfinished")
        raise KeyboardInterrupt

    with monkeypatch.context() as m:
        m.setattr(store, "_export_checkout", interrupted)
        with pytest.raises(KeyboardInterrupt):
            store.checkout_stable(root, dest)
    assert not dest.exists()

    def raced(stage, *args):
        export(stage, *args)
        dest.mkdir()  # A concurrently created empty directory must survive publication.

    with monkeypatch.context() as m:
        m.setattr(store, "_export_checkout", raced)
        with pytest.raises(OSError):
            store.checkout_stable(root, dest)
    assert list(dest.iterdir()) == []
    dest.rmdir()
    assert store.checkout_stable(root, dest)["stable"] == initial["stable"]
