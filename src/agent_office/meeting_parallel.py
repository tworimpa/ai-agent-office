"""Run a disjoint first group of approved tasks and import its checked changes together."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import combinations
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile

from .dialogue import extract_request
from .events import snapshot_request
from .meeting import validate_plan
from .release_store import run_git
from .release_validation import MAX_FILES, MAX_SOURCE, _check_path, committed_files
from .studio_catalog import _git, _redirected, read_workspace_revision
from .team_runs import MAX_ACTIVE


MAX_CHANGE = 1024 * 1024


def _frontier(plan, *, all_independent=False):
    eligible = []
    for item in plan["tasks"]:
        scopes = [PurePosixPath(scope.casefold()) for scope in item.get("folders", [])]
        if item["owner"] != "developer" or item["depends_on"] or not scopes or PurePosixPath(".") in scopes:
            continue
        eligible.append((item, scopes))
    if all_independent:
        group, used = [], []
        for item, scopes in eligible:
            if not any(a.is_relative_to(b) or b.is_relative_to(a) for a in scopes for b in used):
                group.append(item)
                used.extend(scopes)
        return group if len(group) >= 2 else []
    for size in range(min(MAX_ACTIVE, len(eligible)), 1, -1):
        for group in combinations(eligible, size):
            if not any(a.is_relative_to(b) or b.is_relative_to(a)
                       for (_, left), (_, right) in combinations(group, 2) for a in left for b in right):
                return [item for item, _ in group]
    return []


def _contents(root, *, check_name=None):
    """Read bounded regular working files without following redirects or hard links."""
    files, seen, pending = {}, {}, [root]
    total = 0
    entries = 0
    while pending:
        folder = pending.pop()
        for file in folder.iterdir():
            if file == root / ".git":
                continue
            entries += 1
            if entries > MAX_FILES * 2:
                raise ValueError("병렬 작업 폴더가 크기 제한을 초과했습니다.")
            name = file.relative_to(root).as_posix()
            _check_path(name, seen)
            if check_name:
                check_name(name)
            info = file.lstat()
            if _redirected(info):
                raise ValueError("병렬 작업에 링크가 포함되어 있습니다.")
            if stat.S_ISDIR(info.st_mode):
                pending.append(file)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("병렬 작업에는 일반 파일만 사용할 수 있습니다.")
            total += info.st_size
            if total > MAX_SOURCE or len(files) >= MAX_FILES:
                raise ValueError("병렬 작업 파일이 크기 제한을 초과했습니다.")
            with file.open("rb") as handle:
                if not os.path.samestat(os.fstat(handle.fileno()), info):
                    raise ValueError("병렬 작업 파일이 읽는 도중 변경됐습니다.")
                data = handle.read(info.st_size + 1)
            if len(data) != info.st_size:
                raise ValueError("병렬 작업 파일이 읽는 도중 변경됐습니다.")
            files[name] = (0o755 if info.st_mode & stat.S_IXUSR else 0o644, data)
    return files


def _changes(initial, final, folders):
    changes = {}
    total = 0
    for name in sorted(initial.keys() | final.keys()):
        old, new = initial.get(name), final.get(name)
        if old == new:
            continue
        if not any(PurePosixPath(name).is_relative_to(scope) for scope in folders):
            raise ValueError("담당 폴더를 벗어난 변경이 있습니다.")
        if any(part.startswith(".git") for part in name.casefold().split("/")):
            raise ValueError("Git 설정 변경은 병렬 작업에 포함할 수 없습니다.")
        for value in (old, new):
            if value:
                data = value[1]
                total += len(data)
                if total > MAX_CHANGE or b"\0" in data:
                    raise ValueError("병렬 변경이 텍스트 파일 크기 제한을 초과했습니다.")
                data.decode("utf-8")
        changes[name] = new
    if not changes:
        raise ValueError("담당 폴더에 반영할 변경이 없습니다.")
    if len(changes) > 64 or sum(map(len, changes)) > 6000:
        raise ValueError("하위 작업의 변경 파일 수나 경로 길이가 제한을 초과했습니다.")
    return changes


def _worker(adapter, root, item, task, plan, open_inbox, independent):
    initial = _contents(root)
    prompt = ("MEETING_IMPLEMENT\nImplement only the assigned task in the listed folders. "
              "Never commit, push, merge, deploy, change security policy, or edit another folder. "
              "Report work performed. If blocked, stop; do not grant yourself permission.\n"
              + json.dumps({"request": task, "assigned": item, "outcome": plan["outcome"],
                            "criteria": plan["criteria"], "rationale": plan["rationale"]}, ensure_ascii=False))
    inbox = open_inbox(item["id"]) if independent else open_inbox()
    try:
        result = adapter.execute(prompt, workspace=root, allow_edits=True, strict_completion=True, inbox=inbox)
    finally:
        inbox.expire()
    if result.returncode or result.truncated or not result.message.strip():
        raise ValueError(f"담당 엔진 실행 실패 (종료 코드 {result.returncode}).")
    if extract_request(result.message):
        raise ValueError("하위 작업에서 결정을 요청해 변경 적용을 중단했습니다.")
    if read_workspace_revision(root) != plan["evidence"]["revision"]:
        raise ValueError("하위 작업 트리의 기준 커밋이 변경됐습니다.")
    return _changes(initial, _contents(root), item["folders"])


def _check_destination(root, names):
    seen = {}
    for name in names:
        _check_path(name, seen)
        path = root
        for part in PurePosixPath(name).parts:
            path /= part
            try:
                info = path.lstat()
            except FileNotFoundError:
                break
            if _redirected(info) or (not stat.S_ISDIR(info.st_mode)
                                     and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1)):
                raise ValueError("주 작업 트리에 연결된 경로가 있어 변경을 적용할 수 없습니다.")


def _apply(repo, objects, version, changes, temp, tracked_modes):
    # Generate a single patch from host-collected bytes in a private index. No
    # generated command, worker patch text or worker Git index is interpreted.
    index = {"GIT_INDEX_FILE": str(temp / "merged.index")}
    run_git(objects, "read-tree", version, env=index)
    updates = bytearray()
    for name, value in changes.items():
        if value is None:
            mode, oid = "0", b"0" * len(version)
        else:
            permissions, data = value
            # Windows does not expose Git's executable bit through lstat.
            if os.name == "nt":
                permissions = tracked_modes.get(name, 0o644)
            mode = "100755" if permissions == 0o755 else "100644"
            oid = run_git(objects, "hash-object", "-w", "--stdin", data=data).strip()
        updates.extend(mode.encode() + b" " + oid + b"\t" + name.encode() + b"\0")
    run_git(objects, "update-index", "-z", "--index-info", env=index, data=bytes(updates))
    patch = run_git(objects, "diff", "--cached", "--no-renames", "--no-ext-diff", "--no-textconv",
                    "--full-index", version, env=index)
    if not patch or len(patch) > 4 * MAX_CHANGE or b"\nBinary files " in patch:
        raise ValueError("병렬 결과의 패치를 검증할 수 없습니다.")
    _check_destination(repo, changes)
    if read_workspace_revision(repo) != version:
        raise ValueError("병렬 실행 도중 주 작업 트리의 기준 커밋이 변경됐습니다.")
    # The release-store plumbing disables global autocrlf. The target was
    # checked out under the operator's own setting and can contain CRLF.
    autocrlf = _git(repo, "config", "--default=false", "--get", "core.autocrlf",
                    error="주 작업 트리의 줄바꿈 설정을 확인할 수 없습니다.").lower()
    if autocrlf not in ("true", "false", "input", "yes", "no", "on", "off", "0", "1"):
        raise ValueError("주 작업 트리의 줄바꿈 설정을 확인할 수 없습니다.")
    config = ["-c", "core.autocrlf=" + autocrlf]
    if run_git(repo, *config, "--literal-pathspecs", "status", "--porcelain", "-z", "--untracked-files=all",
               "--ignored", "--ignore-submodules=all", "--", *changes):
        raise ValueError("주 작업 트리에 기존 변경이 있어 병렬 결과를 적용할 수 없습니다.")
    run_git(repo, *config, "apply", "--check", "--whitespace=nowarn", "-", data=patch)
    run_git(repo, *config, "apply", "--whitespace=nowarn", "-", data=patch)


def execute_parallel(plan, workspace, adapters, developer, *, task, record, open_inbox, independent=False):
    """Apply one bounded root frontier; all later tasks stay with the primary worker."""
    selected = _frontier(validate_plan(plan), all_independent=independent)
    if not selected:
        return ""
    repo = Path(workspace).resolve(strict=True)
    version = plan["evidence"]["revision"]
    if read_workspace_revision(repo) != version:
        raise ValueError("병렬 작업의 기준 커밋이 승인된 계획과 다릅니다.")
    # Use throwaway worktree metadata. Child processes never share the
    # original repository's refs, index or worktree registration.
    with tempfile.TemporaryDirectory(dir=repo.parent, prefix="office-parallel-") as directory:
        temp = Path(directory)
        objects = temp / "objects.git"
        run_git(temp, "clone", "--bare", "--shared", "--local", "--no-tags", "--", str(repo), str(objects))
        try:
            tracked_modes = {name: mode for name, mode, _ in committed_files(objects, version)}
        except ValueError:
            # Linked, nonportable or oversized source trees continue in the
            # existing sequential workflow before any provider is launched.
            return ""
        (objects / "info").mkdir(exist_ok=True)
        (objects / "info" / "attributes").write_bytes(b"* -text -crlf -ident -filter -working-tree-encoding -eol\n")
        roots = []
        for i, item in enumerate(selected):
            root = temp / f"worker-{i}"
            run_git(objects, "worktree", "add", "--detach", "--", str(root), version)
            roots.append(root)
        changes = {}
        try:
            with ThreadPoolExecutor(max_workers=MAX_ACTIVE) as workers:
                jobs = {}
                for item, root in zip(selected, roots):
                    record(item["id"], "별도 작업 트리에서 병렬 실행을 시작했습니다.")
                    job = workers.submit(_worker, adapters[developer], root, item, task, plan, open_inbox, independent)
                    jobs[job] = item
                errors = []
                for job in as_completed(jobs):
                    item = jobs[job]
                    try:
                        changes[item["id"]] = job.result()
                        if independent:
                            _apply(repo, objects, version, changes[item["id"]], temp, tracked_modes)
                            record(item["id"], "주 작업 트리에 반영: " + ", ".join(changes[item["id"]])[:800])
                    except (OSError, RuntimeError, ValueError) as exc:
                        if independent:
                            record(item["id"], "하위 작업을 반영하지 못했습니다. " + snapshot_request(str(exc))[:600])
                        errors.append(f"{item['id']} 하위 작업을 검증하지 못했습니다: {exc}")
                if errors:
                    raise ValueError(errors[0])
            if not independent:
                combined = {name: data for changed in changes.values() for name, data in changed.items()}
                _apply(repo, objects, version, combined, temp, tracked_modes)
        except (OSError, RuntimeError, ValueError) as exc:
            reason = snapshot_request(str(exc))[:600]
            if not independent:
                for item in selected:
                    record(item["id"], "병렬 결과를 적용하지 못했습니다. " + reason)
            raise ValueError("병렬 작업 실행 또는 결과 반영에 실패했습니다.") from exc
    completed = []
    for item in selected:
        names = list(changes[item["id"]])
        if not independent:
            record(item["id"], "주 작업 트리에 반영: " + ", ".join(names)[:800])
        completed.append({"id": item["id"], "description": item["description"], "changed_files": names})
    remaining = [item for item in plan["tasks"] if item["id"] not in changes]
    return ("The following parallel tasks are already applied in this worktree. Inspect the changes and "
            "carry out remaining developer work in dependency order, preserving reviewer assignments.\n"
            + json.dumps({"applied": completed, "remaining": remaining}, ensure_ascii=False))
