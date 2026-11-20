"""Prove an added regression test fails against the original committed implementation."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .goal_candidates import capture
from .goal_checks import recipe
from .release_store import run_git
from .store import encode, now
from .studio_catalog import normalize_test_files, verify_worktree_test_files


def schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS studio_goal_reproducers(
        run_id TEXT PRIMARY KEY REFERENCES runs(run_id), files_json TEXT NOT NULL,
        check_json TEXT NOT NULL, created_at TEXT NOT NULL)""")


def read(db, run_id):
    row = db.execute("SELECT files_json,check_json,created_at FROM studio_goal_reproducers WHERE run_id=?",
                     (run_id,)).fetchone()
    return (json.loads(row[0]), json.loads(row[1]), row[2]) if row else None


def measure(goal):
    files, _, _ = capture(goal.root, goal.revision, goal.folders)
    if not files:
        raise ValueError("재현할 새 회귀 테스트를 받지 못했습니다.")
    normalize_test_files(files)
    verify_worktree_test_files(goal.root, files)
    if run_git(goal.root, "--literal-pathspecs", "ls-tree", "-r", "--name-only", goal.revision, "--", *files):
        raise ValueError("재현 단계에서는 기존 파일 변경 없이 새 테스트만 추가할 수 있습니다.")
    source = {name:(goal.root / name).read_bytes() for name in files}
    hashes = {name: hashlib.sha256(content).hexdigest() for name, content in source.items()}
    profile = next(c for c in goal.policy["checks"] if c["stage"] == "quick" and recipe(c["argv"]) == "pytest")
    check = dict(profile, argv=["python", "-m", "pytest", "-q", "--", *files])
    # Detached checkout from a pinned commit: an ignored file or side effect in the
    # developer's tree cannot manufacture a baseline failure.
    with TemporaryDirectory(prefix="office-reproduction-") as temp:
        root = Path(temp)
        run_git(root, "init", "-q", *(["--object-format=sha256"] if len(goal.revision) == 64 else []))
        run_git(root, "fetch", "-q", "--no-tags", "--depth=1", str(goal.root), goal.revision)
        run_git(root, "checkout", "-q", "--detach", goal.revision)
        for name, content in source.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("xb") as stream:
                stream.write(content)
        receipt, cases = goal._run(0, len(goal.policy["checks"]), check, workspace=root)
    if receipt["status"] != "failed" or not any(status == "failed" and case.split("::",1)[0] in files
                                                for case, status in cases.items()):
        raise ValueError("원본 코드에서 새 테스트의 실패를 확인하지 못했습니다. 재현 조건을 검토하세요.")
    goal.db.execute("INSERT INTO studio_goal_reproducers VALUES(?,?,?,?)",
                    (goal.id, encode(hashes), encode(check), now()))
    goal.db.execute("DELETE FROM studio_goal_stops WHERE run_id=?", (goal.id,))
    return receipt


def intact(root, hashes):
    verify_worktree_test_files(root, list(hashes))
    return all(hashlib.sha256((root / name).read_bytes()).hexdigest() == digest for name,digest in hashes.items())
