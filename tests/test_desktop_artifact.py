from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from importlib.util import module_from_spec, spec_from_file_location

spec = spec_from_file_location("desktop_artifact", Path(__file__).resolve().parents[1] / "scripts" / "desktop_artifact.py")
artifact = module_from_spec(spec)
spec.loader.exec_module(artifact)


def test_direct_artifact_retries_metadata_and_archive_404_by_id(monkeypatch):
    files = {"setup.exe": b"binary", "SHA256SUMS.txt": b"sums"}
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    body = stream.getvalue()
    digest = sha256(body).hexdigest()
    artifact_id = 14932212147
    endpoint = f"repos/owner/repo/actions/artifacts/{artifact_id}"
    meta = {"id": artifact_id, "name": "electron-installer-Windows", "digest": "sha256:" + digest,
            "workflow_run": {"id": 99}, "expired": False}
    requests = []

    def run(cmd, stdout, stderr):
        path = cmd[-1]
        assert cmd[:-1] == ["gh", "api", "--hostname", "github.com"]
        assert path in [endpoint, endpoint + "/zip"]
        requests.append(path)
        if requests.count(path) == 1:
            stdout.write(b"a partly downloaded response")
            return SimpleNamespace(returncode=1, stderr=b"gh: Not Found (HTTP 404)")
        stdout.write(json.dumps(meta).encode() if path == endpoint else body)
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(artifact.subprocess, "run", run)
    monkeypatch.setattr(artifact.time, "sleep", lambda seconds: None)
    args = {"run_id": 99, "name": meta["name"], "digest": digest, "filenames": files}
    with artifact.verified_artifact("owner/repo", artifact_id, **args) as archive:
        assert {name: archive.read(name) for name in archive.namelist()} == files
    assert requests == [endpoint, endpoint, endpoint + "/zip", endpoint + "/zip"]
    for changed in [{"run_id": 100}, {"name": "other"}, {"digest": "a"*64}, {"filenames": ["setup.exe"]}]:
        with pytest.raises(ValueError), artifact.verified_artifact("owner/repo", artifact_id, **(args | changed)):
            pytest.fail("Accepted mismatched artifact")
    body += b"changed in transit"
    with pytest.raises(ValueError, match="ZIP digest"), artifact.verified_artifact("owner/repo", artifact_id, **args):
        pytest.fail("Accepted mismatched ZIP bytes")
