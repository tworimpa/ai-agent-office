"""Bounded Git status and diff for the disposable worktree."""
import os
from pathlib import Path
import stat

from .events import snapshot_request
from .workspace import read_git_prefix


def _new_source(root, relative):
    # Preview ordinary source files. Hidden, linked and unrecognized files stay in status only.
    path = root / relative
    if (relative.is_absolute() or not relative.parts or any(p.startswith(".") for p in relative.parts)
            or path.suffix.lower() not in {".py", ".js", ".ts", ".tsx", ".jsx", ".html", ".css",
                                          ".rs", ".go", ".java", ".c", ".cpp", ".h", ".md", ".txt"}
            or not str(relative).isprintable()):
        return None
    if any(word in str(relative).casefold() for word in ("secret", "credential", "password", "private_key", "token")):
        return None
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 8192
            or path.resolve(strict=True) != path):
        return None
    with path.open("rb") as file:
        if not os.path.samestat(os.fstat(file.fileno()), info):
            return None
        data = file.read(8193)
    if len(data) > 8192 or b"\0" in data:
        return None
    try:
        content = data.decode("utf-8")
    except UnicodeError:
        return None
    lines = content.splitlines()
    added = "\n".join("+" + line for line in lines)
    return f"--- /dev/null\n+++ b/{relative.as_posix()}\n@@ -0,0 +1,{len(lines)} @@\n{added}\n"


def changes_in(root):
    if root.resolve() != root or not (root / ".git").is_file():
        return "", ""
    status, clipped = read_git_prefix(root, "status", "--short", "--untracked-files=all", "--ignore-submodules=all")
    changes = status.decode("utf-8", errors="replace") + ("\n...[truncated]" if clipped else "")
    raw, clipped = read_git_prefix(root, "diff", "--no-ext-diff", "--no-textconv", "--ignore-submodules=all", "HEAD", "--")
    diff = raw.decode("utf-8", errors="replace")
    if clipped:
        diff += "\n...[truncated]"
    if any(line.startswith("?? ") for line in changes.splitlines()) and len(raw) < 8192:
        names, omitted = read_git_prefix(root, "ls-files", "--others", "--exclude-standard", "-z")
        for name in names.split(b"\0")[:-1]:
            try:
                patch = _new_source(root, Path(os.fsdecode(name)))
            except OSError:
                patch = None
            if patch is None:
                omitted = True
            else:
                diff += patch
            if len(diff) >= 8192:
                break
        if omitted:
            diff += "\n일부 새 파일은 변경 파일 목록과 작업 폴더에서 확인하세요."
    return snapshot_request(changes), snapshot_request(diff)
