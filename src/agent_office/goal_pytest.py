"""Trusted pytest bootstrap for project code in a goal worktree."""
import os
from pathlib import Path
import sys

import pytest


if __name__ == "__main__":
    root = Path.cwd()
    paths = [str(root), str(root / "src")]
    sys.path[:0] = paths
    # Subprocesses spawned by project tests must exercise this candidate too.
    os.environ["PYTHONPATH"] = os.pathsep.join(paths)
    raise SystemExit(pytest.main(sys.argv[1:]))
