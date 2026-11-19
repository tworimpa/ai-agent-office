"""Trusted Agent Office regression suite, run as the image entrypoint."""

import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile


def main():
    with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
        root = Path(temporary)
        with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as archive:
            for member in archive:
                if not member.isreg():
                    raise ValueError("Expected a regular source file")
                archive.extract(member, root, filter="data")

        sources = sorted((root / "src" / "agent_office").rglob("*.py"))
        if not sources:
            raise ValueError("Missing Agent Office Python sources")
        for path in sources:
            compile(path.read_bytes(), str(path.relative_to(root)), "exec")
        print(f"Compiled {len(sources)} Python sources.", flush=True)

        valid_id = "0123456789abcdef" * 2
        cases = [
            ("valid run ID", "id", valid_id, ["returned", valid_id]),
            ("invalid run ID", "id", "xyz", ["rejected"]),
            ("event metadata", "payload", {"phase": "run", "stdout": "private"},
             ["returned", {"phase": "run"}]),
            ("unknown engine", "payload", {"engine": "unknown"}, ["rejected"]),
            ("typed attempt", "payload", {"attempt": True}, ["rejected"]),
            ("request redaction", "request", "TOKEN=private", ["returned", "TOKEN=[redacted]"]),
        ]
        for label, operation, value, expected in cases:
            probe = subprocess.run(
                [sys.executable, "-I", "-B", "/opt/validator/probe.py"], cwd=root,
                input=json.dumps([operation, value]).encode(), capture_output=True, timeout=45,
            )
            if probe.returncode or probe.stderr or json.loads(probe.stdout) != expected:
                raise ValueError(f"Failed regression: {label}")
            print(f"Passed: {label}", flush=True)


if __name__ == "__main__":
    main()
