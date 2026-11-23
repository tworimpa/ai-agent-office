import os
from pathlib import Path
import platform
import subprocess
import sqlite3
import sys
from uuid import uuid4

from agent_office.models import OwnerInfo, TaskSpec
from agent_office.ownership import RunGuard
from agent_office.release_store import run_git
from agent_office.store import TaskStore
from agent_office.studio_ssh import _check_reply, is_peer_route
from agent_office.worker_environment import coding_environment
from agent_office.workspace import create_worktree, plan_worktree
from agent_office import worktree_storage
from test_studio_http import project, request, studio


def seed(server, repo, workspace, state="PLAN_ONLY"):
    run = uuid4().hex
    tree = create_worktree(repo, planned=plan_worktree(repo, run))
    task = TaskSpec(uuid4().hex, str(repo), "fixture", "codex", "antigravity", 1)
    with TaskStore(server.db_path) as store:
        store.create_run(task, run, tree, OwnerInfo(platform.node(), uuid4().hex))
        db = store.connection
        db.execute("INSERT INTO studio_runs(id,workspace_id,folders,allow_edits,request_hash) VALUES (?,?,'[]',0,'test')",
                   (run, workspace))
        db.execute("UPDATE runs SET state=?,ended_at='2000-01-01T00:00:00+00:00' WHERE run_id=?", (state, run))
    return run, tree


def test_storage_http_rechecks_git_journal_locks_and_removes_only_selected(tmp_path, monkeypatch):
    web, _ = project(tmp_path)
    repo = web.parent
    with studio(tmp_path / "journal.db") as server:
        workspace = server.catalog.create("fixture", [str(web)])["id"]
        other = server.catalog.create("other", [str(web)])["id"]
        run, tree = seed(server, repo, workspace)
        protected, kept = seed(server, repo, workspace, "READY_FOR_HUMAN_REVIEW")
        route = "/rooms/" + workspace + "/storage"
        path = "/api" + route
        assert is_peer_route("GET", route) and is_peer_route("POST", route)
        status, preview = request(server, path)
        assert status == 200, preview
        _check_reply(status, preview, [server.journal_id.encode()], server.journal_id, "GET", route, None)
        by_run = {row["run_id"]: row for row in preview["entries"]}
        assert by_run[run]["reasons"] == [] and by_run[run]["bytes"] > 0
        assert by_run[protected]["reasons"] == ["review"]
        body = {"run_id": run, "head": by_run[run]["head"], "journal_id": server.journal_id}
        assert request(server, path, body | {"path": str(repo)})[0] == 400
        assert request(server, path.replace(workspace, other), body)[0] == 404
        assert request(server, path, body | {"journal_id": other})[0] == 400
        with RunGuard.worker(server.db_path, run):
            assert request(server, path, body)[1]["outcome"] == "locked"
        assert request(server, path, body | {"head": "a"*40})[1]["outcome"] == "changed"
        (tree.path / "preserve.txt").write_text("mine")
        assert request(server, path, body)[1]["outcome"] == "dirty"
        assert (tree.path / "preserve.txt").read_text() == "mine"
        (tree.path / "preserve.txt").unlink()
        # Interrupt branch cleanup once. Retry must revalidate both journal and tip.
        original = worktree_storage._git
        with monkeypatch.context() as m:
            def refuse_branch(cwd, *args, **kwargs):
                if args[0] == "update-ref":
                    raise ValueError("fixture")
                return original(cwd, *args, **kwargs)
            m.setattr(worktree_storage, "_git", refuse_branch)
            assert request(server, path, body)[1]["outcome"] == "partial"
        assert not tree.path.exists()
        assert request(server, path, body)[1]["outcome"] == "removed"
        assert request(server, path, body)[1]["outcome"] == "removed"
        assert not run_git(repo, "for-each-ref", "refs/heads/" + tree.branch)
        assert kept.path.is_dir() and (repo / "web" / "value.txt").is_file()


def test_storage_preserves_unmerged_ignored_and_checkpoint(tmp_path):
    web, _ = project(tmp_path)
    repo = web.parent
    with studio(tmp_path / "journal.db") as server:
        workspace = server.catalog.create("fixture", [str(web)])["id"]
        run, tree = seed(server, repo, workspace)
        with TaskStore(server.db_path) as store:
            store.connection.execute("""INSERT INTO studio_goal_checkpoints
                (run_id,stage,state_json,resume_phase,seq,created_at) VALUES (?,'test','{}','test',1,'now')""", (run,))
        run_git(tree.path, "commit", "--allow-empty", "-m", "fixture extra")
        (repo / ".git" / "info" / "exclude").write_text("keep.log\n")
        (tree.path / "keep.log").write_text("local ignored data")
        view = worktree_storage.handle(server, workspace, None)
        row, = view["entries"]
        assert set(row["reasons"]) == {"checkpoint", "dirty", "unmerged"}
        assert row["bytes"] >= len("local ignored data")
        body = {"run_id": run, "head": row["head"], "journal_id": server.journal_id}
        assert worktree_storage.handle(server, workspace, body)["outcome"] in row["reasons"]
        assert (tree.path / "keep.log").exists()


def test_coding_env_preserves_local_git_and_blocks_fixture_transports_and_gh(tmp_path):
    web, _ = project(tmp_path)
    repo = web.parent
    bare = tmp_path / "mock-upstream.git"
    bare.mkdir()
    run_git(bare, "init", "--bare")
    run_git(repo, "remote", "add", "origin", str(bare))
    mock_tools = tmp_path / "mock-tools"
    mock_tools.mkdir()
    if os.name == "nt":
        (mock_tools / "gh.cmd").write_bytes(b"@echo fixture>mock-gh-called\r\n@exit /b 0\r\n")
        command = ["cmd", "/d", "/c", "gh", "--version"]
    else:
        stub = mock_tools / "gh"
        stub.write_bytes(b"#!/bin/sh\necho fixture > mock-gh-called\n")
        stub.chmod(0o700)
        command = ["gh", "--version"]
    source = dict(os.environ, PATH=str(mock_tools) + os.pathsep + os.environ["PATH"], GH_TOKEN="mock")
    subprocess.run(command, cwd=repo, env=source, capture_output=True, check=True)
    marker = repo / "mock-gh-called"
    assert marker.read_text().strip() == "fixture"
    marker.unlink()
    with coding_environment(source) as env:
        assert "GH_TOKEN" not in env and list(Path(env["GH_CONFIG_DIR"]).iterdir()) == []
        assert subprocess.run(["git", "status", "--porcelain"], cwd=repo, env=env, capture_output=True).returncode == 0
        if os.name == "nt":
            # Some Windows launchers remove zero-length environment entries.
            # Invoke a real .cmd shim and have the next process rebuild its env.
            probe = tmp_path / "env_probe.py"
            probe.write_text(
                "import os, subprocess, sys\n"
                "env = {k:v for k,v in os.environ.items() if v}\n"
                "def git(*args): return subprocess.run(['git',*args],env=env,capture_output=True)\n"
                "assert git('config','--get','credential.helper').stdout.strip() == b''\n"
                "assert git('init','-q',sys.argv[2]).returncode == 0\n"
                "assert git('push',sys.argv[1],'HEAD:refs/heads/candidate').returncode != 0\n")
            shim = tmp_path / "check-env.cmd"
            shim.write_text(f'@"{sys.executable}" "{probe}" "{bare}" "{tmp_path / "nested"}"\n')
            subprocess.run([str(shim)], cwd=repo, env=env, capture_output=True, check=True)
        for target in ("origin", str(bare)):
            denied = subprocess.run(["git", "push", target, "HEAD:refs/heads/candidate"], cwd=repo, env=env,
                                    capture_output=True)
            assert denied.returncode != 0
        assert subprocess.run(command, cwd=repo, env=env, capture_output=True).returncode != 0
        assert not marker.exists()
    assert not Path(env["GH_CONFIG_DIR"]).exists()
    assert source["GH_TOKEN"] == "mock" and not run_git(bare, "for-each-ref")
    run_git(repo, "push", "origin", "HEAD:refs/heads/authorized-fixture")
    assert run_git(bare, "rev-parse", "refs/heads/authorized-fixture") == run_git(repo, "rev-parse", "HEAD")


def test_cli_preview_excludes_other_projects_and_requires_owned_paths_and_drained_workers(tmp_path):
    web, contracts = project(tmp_path)
    with studio(tmp_path / "journal.db") as server:
        workspace = server.catalog.create("fixture", [str(web)])["id"]
        run, tree = seed(server, web.parent, workspace)
        with TaskStore(server.db_path) as store:
            db = store.connection
            db.execute("DELETE FROM studio_runs WHERE id=?", (run,))
            unrelated = tmp_path / "other-project"
            unrelated.mkdir()
            run_git(unrelated, "init")
            run_git(unrelated, "commit", "--allow-empty", "-m", "fixture")
            other = server.catalog.create("other", [str(unrelated)])["id"]
            assert not worktree_storage.handle(server, other, None)["entries"]
            row, = worktree_storage.handle(server, workspace, None)["entries"]
            assert row["run_id"] == run and row["reasons"] == []
            body = {"run_id": run, "head": row["head"], "journal_id": server.journal_id}
            db.execute("UPDATE runs SET state='RECOVERY_REQUIRED' WHERE run_id=?", (run,))
            assert worktree_storage.handle(server, workspace, body)["outcome"] == "recovery"
            db.execute("UPDATE runs SET state='RUNNING',ended_at=NULL WHERE run_id=?", (run,))
            assert worktree_storage.handle(server, workspace, body)["outcome"] == "active"
            db.execute("UPDATE runs SET state='PLAN_ONLY',ended_at='2000-01-01T00:00:00+00:00' WHERE run_id=?", (run,))
            db.execute("INSERT INTO studio_goal_workers(holder,run_id,task_id,started_at) VALUES ('child',?,'fixture','now')", (run,))
            assert worktree_storage.handle(server, workspace, body)["outcome"] == "child"
            db.execute("DELETE FROM studio_goal_workers WHERE holder='child'")
            db.execute("UPDATE tasks SET workspace=? WHERE current_run_id=?", (str(contracts), run))
            assert worktree_storage.handle(server, workspace, body)["outcome"] == "identity"
        assert (contracts / "wanted.txt").is_file() and tree.path.is_dir()


def test_branch_compare_and_delete_rejects_changed_tip_and_blocks_checkout_during_delete(tmp_path, monkeypatch):
    web, _ = project(tmp_path)
    repo = web.parent
    with studio(tmp_path / "journal.db") as server:
        workspace = server.catalog.create("fixture", [str(web)])["id"]
        run, tree = seed(server, repo, workspace)
        _, other = seed(server, repo, workspace, "READY_FOR_HUMAN_REVIEW")
        view = worktree_storage.handle(server, workspace, None)
        old = next(row["head"] for row in view["entries"] if row["run_id"] == run)
        new = run_git(repo, "commit-tree", old + "^{tree}", "-p", old, "-m", "concurrent fixture").decode().strip()
        ref = "refs/heads/" + tree.branch
        attempts = []
        original = worktree_storage._git

        def move_during_delete(cwd, *args, **kwargs):
            if args[:3] == ("update-ref", "--no-deref", "-d"):
                switch = subprocess.run(["git", "switch", tree.branch], cwd=other.path, capture_output=True)
                attempts.append(switch.returncode)
                run_git(repo, "update-ref", ref, new, old)
            return original(cwd, *args, **kwargs)

        with monkeypatch.context() as m:
            m.setattr(worktree_storage, "_git", move_during_delete)
            body = {"run_id": run, "head": old, "journal_id": server.journal_id}
            assert worktree_storage.handle(server, workspace, body)["outcome"] == "partial"
        assert len(attempts) == 1 and attempts[0] != 0
        assert run_git(repo, "rev-parse", ref).decode().strip() == new
        assert run_git(other.path, "symbolic-ref", "--short", "HEAD").decode().strip() == other.branch


def test_new_checkout_registration_during_branch_deletion_preserves_the_ref(tmp_path, monkeypatch):
    web, _ = project(tmp_path)
    repo = web.parent
    with studio(tmp_path / "journal.db") as server:
        workspace = server.catalog.create("fixture", [str(web)])["id"]
        run, tree = seed(server, repo, workspace)
        row, = worktree_storage.handle(server, workspace, None)["entries"]
        body = {"run_id": run, "head": row["head"], "journal_id": server.journal_id}
        original = worktree_storage._git
        checkout = tmp_path / "user checkout"

        def register_before_delete(cwd, *args):
            if args[:3] == ("update-ref", "--no-deref", "-d"):
                run_git(repo, "worktree", "add", str(checkout), tree.branch)
            return original(cwd, *args)

        monkeypatch.setattr(worktree_storage, "_git", register_before_delete)
        assert worktree_storage.handle(server, workspace, body)["outcome"] == "partial"
        assert run_git(checkout, "symbolic-ref", "--short", "HEAD").decode().strip() == tree.branch
        assert run_git(checkout, "rev-parse", "HEAD").decode().strip() == row["head"]


def test_preview_releases_sqlite_reader_during_disk_scan(tmp_path, monkeypatch):
    web, _ = project(tmp_path)
    with studio(tmp_path / "journal.db") as server:
        workspace = server.catalog.create("fixture", [str(web)])["id"]
        first, _ = seed(server, web.parent, workspace)
        seed(server, web.parent, workspace)
        with sqlite3.connect(server.db_path) as writer:
            assert writer.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
        measured = worktree_storage._usage
        commits = []

        def measure(root):
            with sqlite3.connect(server.db_path, timeout=0.15) as writer:
                writer.execute("UPDATE studio_runs SET diagnostic='writer fixture' WHERE id=?", (first,))
            commits.append(root)
            return measured(root)

        monkeypatch.setattr(worktree_storage, "_usage", measure)
        report = worktree_storage.handle(server, workspace, None)
        assert len(report["entries"]) == len(commits) == 2


def test_new_ignored_file_moved_to_quarantine_is_reported_and_new_original_is_untouched(tmp_path, monkeypatch):
    web, _ = project(tmp_path)
    repo = web.parent
    (repo / ".git" / "info" / "exclude").write_text("late.log\n")
    with studio(tmp_path / "journal.db") as server:
        workspace = server.catalog.create("fixture", [str(web)])["id"]
        run, tree = seed(server, repo, workspace)
        row, = worktree_storage.handle(server, workspace, None)["entries"]
        body = {"run_id": run, "head": row["head"], "journal_id": server.journal_id}
        original = worktree_storage._git

        def concurrent_file(cwd, *args):
            if args[:2] != ("worktree", "move"):
                return original(cwd, *args)
            (tree.path / "late.log").write_text("retained content")
            result = original(cwd, *args)
            tree.path.mkdir()
            (tree.path / "replacement").write_text("this is a new directory")
            return result

        with monkeypatch.context() as m:
            m.setattr(worktree_storage, "_git", concurrent_file)
            assert worktree_storage.handle(server, workspace, body)["outcome"] == "dirty"
        item, = worktree_storage.handle(server, workspace, None)["entries"]
        kept = Path(item["location"])
        assert kept.parents[2] == tree.path.parent and kept != tree.path
        assert (kept / "late.log").read_text() == "retained content"
        assert (tree.path / "replacement").is_file()
        (kept / "late.log").unlink()
        assert worktree_storage.handle(server, workspace, body)["outcome"] == "removed"
        assert (tree.path / "replacement").is_file()


def test_crash_between_rename_and_git_registration_is_recoverable_after_restart(tmp_path, monkeypatch):
    web, _ = project(tmp_path)
    repo = web.parent
    db = tmp_path / "journal.db"
    with studio(db) as server:
        workspace = server.catalog.create("fixture", [str(web)])["id"]
        run, tree = seed(server, repo, workspace)
        row, = worktree_storage.handle(server, workspace, None)["entries"]
        body = {"run_id": run, "head": row["head"], "journal_id": server.journal_id}
        original = worktree_storage._git

        def interrupt(cwd, *args):
            if args[:2] == ("worktree", "move"):
                os.rename(args[-2], args[-1])
                raise OSError("interrupted before Git registration update")
            return original(cwd, *args)
        with monkeypatch.context() as m:
            m.setattr(worktree_storage, "_git", interrupt)
            assert worktree_storage.handle(server, workspace, body)["outcome"] == "partial"
    with studio(db) as server:
        item, = worktree_storage.handle(server, workspace, None)["entries"]
        assert item["reasons"] == ["interrupted_move"] and Path(item["location"]).is_dir()
        assert worktree_storage.handle(server, workspace, body)["outcome"] == "removed"
        assert repo.is_dir() and not tree.path.exists()


def test_writer_retaining_original_checkout_survives_the_last_rmdir(tmp_path, monkeypatch):
    web, _ = project(tmp_path)
    repo = web.parent
    (repo / ".git" / "info" / "exclude").write_text("late.log\n")
    with studio(tmp_path / "journal.db") as server:
        workspace = server.catalog.create("fixture", [str(web)])["id"]
        run, tree = seed(server, repo, workspace)
        row, = worktree_storage.handle(server, workspace, None)["entries"]
        body = {"run_id": run, "head": row["head"], "journal_id": server.journal_id}
        rename = worktree_storage._restore_interrupted_move
        # On POSIX the open descriptor models an editor retaining its old cwd
        # across rename. Windows directories in use can reject the move itself.
        held = os.open(tree.path, os.O_DIRECTORY) if os.name != "nt" else None

        def write_after_final_check(source, target):
            rename(source, target)
            if source.name == ".git":
                if held is None:
                    (source.parent / "late.log").write_text("unsaved editor buffer")
                else:
                    fd = os.open("late.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=held)
                    with os.fdopen(fd, "w") as file:
                        file.write("unsaved editor buffer")

        try:
            with monkeypatch.context() as m:
                m.setattr(worktree_storage, "_restore_interrupted_move", write_after_final_check)
                assert worktree_storage.handle(server, workspace, body)["outcome"] == "dirty"
        finally:
            if held is not None:
                os.close(held)
        item, = worktree_storage.handle(server, workspace, None)["entries"]
        kept = Path(item["location"])
        assert "dirty" in item["reasons"] and (kept / "late.log").read_text() == "unsaved editor buffer"
        assert (kept / ".git").is_file() and run_git(repo, "for-each-ref", "refs/heads/" + tree.branch)
        (kept / "late.log").unlink()
        assert worktree_storage.handle(server, workspace, body)["outcome"] == "removed"
