from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sqlite3
import subprocess

import pytest

from agent_office.studio_catalog import StudioCatalog


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "한글 repo"
    path.mkdir()
    git(path, "init")
    for name in ("앱 개발", "tests"):
        (path / name).mkdir()
        (path / name / "base").write_text("base")
    git(path, "add", ".")
    git(path, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "base")
    return path


def test_restart_selection_order_and_existing_db(tmp_path, repo):
    journal = tmp_path / "이력 파일.db"
    with sqlite3.connect(journal) as old:
        old.execute("CREATE TABLE legacy(value)")
    before = journal.read_bytes()
    docs = tmp_path / "자료"
    docs.mkdir()
    folders = [str(repo / "앱 개발"), str(docs), str(repo / "tests"), str(repo)]
    created = StudioCatalog(journal).create(" 주문 서비스 ", folders)
    assert created == {"id": created["id"], "name": "주문 서비스", "folders": folders}
    reopened = StudioCatalog(journal)
    assert reopened.list() == [created]
    assert reopened.select(created["id"], [2, 0, 3]) == (repo, ["tests", "앱 개발", "."])
    with pytest.raises(ValueError, match="Git"):
        reopened.select(created["id"], [1])
    assert journal.read_bytes() == before


def test_cross_checkout_staged_and_missing_folders(tmp_path, repo):
    other = tmp_path / "다른 checkout"
    git(repo, "worktree", "add", "--detach", str(other))
    staged = repo / "새 폴더"
    staged.mkdir()
    (staged / "file").touch()
    git(repo, "add", ".")
    c = StudioCatalog(tmp_path / "tasks.db")
    workspace = c.create("프로젝트", [str(other), str(repo), str(staged), str(repo / "tests")])
    with pytest.raises(ValueError, match="저장소"):
        c.select(workspace["id"], [0, 1])
    with pytest.raises(ValueError, match="커밋"):
        c.select(workspace["id"], [2])
    (repo / "tests").rename(repo / "moved")
    with pytest.raises(ValueError, match="폴더"):
        c.select(workspace["id"], [3])


def test_unborn_repo_and_registration_validation(tmp_path):
    repo = tmp_path / "new"
    repo.mkdir()
    git(repo, "init")
    file = tmp_path / "file"
    file.touch()
    c = StudioCatalog(tmp_path / "tasks.db")
    for paths in ([], str(repo), [1], [""], ["."], [str(file)], [str(tmp_path / "missing")],
                  [str(repo), str(repo)], [str(repo / ".." / repo.name)]):
        with pytest.raises(ValueError):
            c.create("test", paths)
    for name in (False, "", " ", "bad\nname"):
        with pytest.raises(ValueError):
            c.create(name, [str(repo)])
    assert c.list() == []
    workspace = c.create("test", [str(repo)])
    with pytest.raises(ValueError, match="커밋"):
        c.select(workspace["id"], [0])
    for indices in ([], [-1], [1], "0", [True], [0.0], [["0"]], [0, 0]):
        with pytest.raises(ValueError):
            c.select(workspace["id"], indices)
    with pytest.raises(ValueError):
        c.select("0" * 32, [0])


def make_directory_link(link: Path, target: Path):
    if os.name == "nt":
        # Directory junctions work on Windows without developer-mode privileges.
        subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(link), str(target)],
                       check=True, capture_output=True)
    else:
        link.symlink_to(target, target_is_directory=True)


def test_link_in_parent_chain_and_replacement(tmp_path, repo):
    c = StudioCatalog(tmp_path / "tasks.db")
    real_folder = repo / "tests"
    saved = c.create("project", [str(real_folder)])
    link = tmp_path / "shortcut"
    make_directory_link(link, repo)
    with pytest.raises(ValueError, match="링크"):
        c.create("link", [str(link / "앱 개발")])
    moved = repo / "new place"
    real_folder.rename(moved)
    make_directory_link(real_folder, moved)
    with pytest.raises(ValueError, match="링크"):
        c.select(saved["id"], [0])


def test_catalog_concurrent_creates_use_independent_connections(tmp_path):
    journal = tmp_path / "new" / "journal.db"

    def register(n):
        return StudioCatalog(journal).create(f"워크스페이스 {n}", [str(tmp_path)])

    with ThreadPoolExecutor(max_workers=8) as pool:
        created = list(pool.map(register, range(20)))
    assert {w["id"] for w in created} == {w["id"] for w in StudioCatalog(journal).list()}
    assert len({w["id"] for w in created}) == 20
    assert not journal.exists()


def test_unicode_spacing_and_emoji_are_preserved(tmp_path, repo):
    folder = repo / "개발자 👩‍💻 앱"
    folder.mkdir()
    (folder / "file").touch()
    git(repo, "add", ".")
    git(repo, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "app")
    c = StudioCatalog(tmp_path / "tasks.db")
    created = c.create(folder.name, [str(folder)])
    assert c.list() == [created]
    assert c.select(created["id"], [0]) == (repo, [folder.name])


def test_committed_file_replaced_by_directory_is_unselectable(tmp_path, repo):
    former_file = repo / "tests" / "base"
    former_file.unlink()
    former_file.mkdir()
    c = StudioCatalog(tmp_path / "tasks.db")
    created = c.create("작업", [str(former_file)])
    with pytest.raises(ValueError, match="커밋"):
        c.select(created["id"], [0])


def test_sidecar_cannot_modify_hardlinked_journal(tmp_path):
    journal = tmp_path / "tasks.db"
    with sqlite3.connect(journal) as db:
        db.execute("CREATE TABLE legacy(id)")
    before = journal.read_bytes()
    (tmp_path / "tasks.db.studio.sqlite3").hardlink_to(journal)
    with pytest.raises(ValueError):
        StudioCatalog(journal)
    assert journal.read_bytes() == before
