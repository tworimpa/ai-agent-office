import subprocess

from agent_office.workspace import create_worktree


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def test_worktree_is_separate_from_source(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("base\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "init")
    worktree = create_worktree(repo)
    assert worktree.path != repo
    assert worktree.path.exists()
    assert worktree.branch.startswith("ao/task-")
    assert (worktree.path / "README.md").read_text() == "base\n"
    (worktree.path / "README.md").write_text("changed\n")
    assert (repo / "README.md").read_text() == "base\n"
