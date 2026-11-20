"""Load the bundled pytest before giving the test process its repository imports."""

import sys
from pathlib import Path

import pytest


def main():
    sys.path.insert(0, str(Path.cwd()))
    return pytest.main(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
