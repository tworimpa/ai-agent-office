"""Download an Actions ZIP by immutable REST artifact ID and verify it before extraction."""

import argparse
from contextlib import contextmanager
from hashlib import file_digest
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from zipfile import ZipFile


def _download(endpoint, stream):
    for attempt in range(5):
        stream.seek(0)
        stream.truncate()
        result = subprocess.run(["gh", "api", "--hostname", "github.com", endpoint],
                                stdout=stream, stderr=subprocess.PIPE)
        if result.returncode == 0:
            stream.seek(0)
            return
        status = re.search(rb"\(HTTP (\d{3})\)", result.stderr)
        code = int(status[1]) if status else None
        if attempt == 4 or code not in (404, 408, 429, 500, 502, 503, 504):
            raise RuntimeError(f"GitHub artifact request failed: {endpoint} (gh exit {result.returncode}, HTTP {code})")
        time.sleep(2 ** attempt)


@contextmanager
def verified_artifact(repository, artifact_id, *, run_id, name, digest, filenames):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Invalid GitHub repository")
    if not (type(artifact_id) is int and artifact_id > 0 and type(run_id) is int and run_id > 0):
        raise ValueError("Expected a positive artifact ID and run ID")
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("Expected the upload's SHA-256 ZIP digest")
    names = sorted(filenames)
    if not names or len(names) != len(set(names)) or not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]*", n) for n in names):
        raise ValueError("Expected unique top-level filenames")
    endpoint = f"repos/{repository}/actions/artifacts/{artifact_id}"
    with tempfile.TemporaryFile() as stream:
        _download(endpoint, stream)
        meta = json.load(stream)
        if (meta["id"] != artifact_id or meta["name"] != name or meta["expired"]
                or meta["workflow_run"]["id"] != run_id or meta["digest"] != "sha256:" + digest):
            raise ValueError(f"Artifact {artifact_id} metadata differs from the expected upload")
        _download(endpoint + "/zip", stream)
        if file_digest(stream, "sha256").hexdigest() != digest:
            raise ValueError(f"Artifact {artifact_id} ZIP digest differs from the expected upload")
        stream.seek(0)
        with ZipFile(stream) as archive:
            if sorted(archive.namelist()) != names:
                raise ValueError(f"Unexpected entries in artifact {artifact_id}")
            yield archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    env = os.environ
    names = (env["INSTALLER_NAME"], "SHA256SUMS.txt")
    with verified_artifact(env["GITHUB_REPOSITORY"], int(env["ARTIFACT_ID"]), run_id=int(env["GITHUB_RUN_ID"]),
                           name="electron-installer-" + env["RUNNER_OS"], digest=env["ARTIFACT_DIGEST"], filenames=names) as archive:
        args.directory.mkdir()
        for name in names:
            with archive.open(name) as source, (args.directory / name).open("xb") as target:
                shutil.copyfileobj(source, target)


if __name__ == "__main__":
    main()
