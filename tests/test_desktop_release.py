"""Release publishing contracts against a local GitHub API and ZIP fixture."""

from base64 import b64encode
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import importlib.util
from io import BytesIO
import json
from pathlib import Path
import re
import sys
from urllib.error import HTTPError
from urllib.request import Request
from zipfile import ZipFile

import pytest


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "desktop_release.py"
spec = importlib.util.spec_from_file_location("desktop_release", MODULE)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

COMMIT = "a" * 40
VERSION = "0.8.2"
TAG = "v" + VERSION
START = "2026-11-01T15:00:00Z"
END = "2026-11-01T15:05:00Z"
CREATED = "2026-11-01T15:04:00Z"


def test_release_gate_requires_the_same_checks_as_the_deployment_policy():
    from agent_office import policy_github

    assert release.WORKFLOWS == {
        "ci.yml": {"ubuntu-latest / Python 3.11", "ubuntu-latest / Python 3.13",
                   "windows-latest / Python 3.11", "windows-latest / Python 3.13"},
        "desktop.yml": {"ubuntu-latest desktop installer", "windows-latest desktop installer",
                        "tested-merge / ubuntu-latest / Tested merge"},
    }
    assert {".github/workflows/" + name: checks for name, checks in release.WORKFLOWS.items()} == {
        path: checks for path, (_, checks) in policy_github.WORKFLOWS.items()}


def test_010_release_notes_read_highlights_from_the_pinned_commit():
    content = b"### Release highlights\r\n\r\n- Catalog and bot sharing\r\n"

    class Source:
        repo = "owner/project"

        def get(self, path):
            assert path == f"contents/docs/releases/0.10.0.md?ref={COMMIT}"
            return {"content": b64encode(content).decode("ascii")}

    notes = release.release_notes(Source(), "0.10.0", COMMIT, {"workflows": {}, "packages": {}})
    assert "### Release highlights\n\n- Catalog and bot sharing\n\n### 빌드 검증" in notes
    assert "AI-Agent-Office-Setup-0.10.0.exe" in notes


def packed(files):
    result = BytesIO()
    with ZipFile(result, "w") as archive:
        for name, contents in files.items():
            archive.writestr(name, contents)
    return result.getvalue()


class Repository:
    repo = "owner/project"

    def __init__(self):
        self.runs = {}
        self.jobs = {}
        for run_id, (workflow, names) in enumerate(release.WORKFLOWS.items(), start=10):
            self.runs[workflow] = {"id": run_id, "head_sha": COMMIT, "path": f".github/workflows/{workflow}",
                                   "event": "push", "head_branch": "main", "run_attempt": 2,
                                   "repository": {"full_name": self.repo}, "head_repository": {"full_name": self.repo},
                                   "status": "completed", "conclusion": "success"}
            self.jobs[run_id] = [{"name": name, "run_attempt": 2, "run_id": run_id, "head_sha": COMMIT, "id": i,
                                  "status": "completed", "conclusion": "success", "started_at": START, "completed_at": END}
                                 for i, name in enumerate(sorted(names), start=100)]
        self.original_jobs = [dict(j, run_attempt=1) for j in self.jobs[11]]
        self.artifacts = {}
        self.payloads = {}
        self.receipts = {}
        for platform, artifact_id, attempt in [("Windows", 30, 1), ("Linux", 40, 2)]:
            name = release.installer_names(VERSION)[platform]
            if platform == "Windows":
                name = f"AI Agent Office Setup {VERSION}.exe"
            installer = (platform + " test installer bytes").encode()
            digest = sha256(installer).hexdigest()
            package = packed({name: installer, release.CHECKSUMS: release.checksum_bytes({name: digest})})
            self.add(artifact_id, "electron-installer-" + platform, package)
            self.receipts[platform] = {"schema_version": 1, "platform": platform, "version": VERSION,
                                       "run_id": 11, "run_attempt": attempt, "head_sha": COMMIT,
                                       "installer_name": name, "installer_sha256": digest, "artifact_id": artifact_id,
                                       "artifact_digest": sha256(package).hexdigest()}
            self.add(artifact_id + 1, f"desktop-receipt-{platform}-{attempt}",
                     packed({"receipt.json": json.dumps(self.receipts[platform])}))
        # A same-name archive from an older attempt must never be selected by name.
        self.add(29, "electron-installer-Linux", b"unrelated archive")
        self.artifacts[29]["created_at"] = "2026-10-31T15:00:00Z"
        self.ref = None
        self.release = None
        self.assets = {}
        self.asset_payloads = {}
        self.writes = []
        self.fail_upload = False

    def add(self, artifact_id, name, payload):
        self.artifacts[artifact_id] = {"name": name, "id": artifact_id, "expired": False, "created_at": CREATED,
                                       "digest": "sha256:" + sha256(payload).hexdigest(),
                                       "workflow_run": {"id": 11, "head_sha": COMMIT, "head_branch": "main",
                                                        "repository_id": 2, "head_repository_id": 2}}
        self.payloads[artifact_id] = payload

    def items(self, url, key=None):
        path = url.partition("?")[0]
        parts = path.split("/")
        if path.startswith("actions/workflows/"):
            assert f"head_sha={COMMIT}" in url and "event=push" in url and "branch=main" in url
            return [self.runs[parts[2]]]
        if path.endswith("/jobs"):
            return self.original_jobs if "attempts" in parts else self.jobs[int(parts[2])]
        if path.endswith("/artifacts"):
            return list(self.artifacts.values())
        if path.startswith("git/matching-refs/"):
            return [{"ref": f"refs/tags/{TAG}", "object": {"type": "commit", "sha": self.ref}}] if self.ref else []
        if path == "releases":
            return [self.release] if self.release else []
        if path == "releases/80/assets":
            return list(self.assets.values())
        raise AssertionError(url)

    def get(self, path):
        if path == f"contents/desktop/package.json?ref={COMMIT}":
            return {"content": b64encode(json.dumps({"version": VERSION}).encode()).decode()}
        if path.startswith("compare/"):
            return {"status": "ahead"}
        if path.startswith("actions/artifacts/"):
            return self.artifacts[int(path.split("/")[-1])]
        if path == "releases/80":
            return deepcopy(self.release)
        raise AssertionError(path)

    def download(self, path, dest):
        if match := re.fullmatch(r"actions/artifacts/(\d+)/zip", path):
            dest.write_bytes(self.payloads[int(match[1])])
        else:
            asset_id = int(re.fullmatch(r"releases/assets/(\d+)", path)[1])
            dest.write_bytes(self.asset_payloads[asset_id])

    @contextmanager
    def verified_artifact(self, artifact_id, *, run_id, name, digest, filenames):
        metadata = self.artifacts[artifact_id]
        assert metadata["workflow_run"]["id"] == run_id and metadata["name"] == name
        data = self.payloads[artifact_id]
        assert digest == sha256(data).hexdigest()
        with ZipFile(BytesIO(data)) as archive:
            assert sorted(archive.namelist()) == sorted(filenames)
            yield archive

    def write(self, method, path, body):
        self.writes.append((method, path))
        if (method, path) == ("POST", "git/refs"):
            self.ref = body["sha"]
            return {}
        if (method, path) == ("POST", "releases"):
            self.release = dict(body, id=80)
        elif (method, path) == ("PATCH", "releases/80"):
            self.release.update(body)
        else:
            raise AssertionError(path)
        return deepcopy(self.release)

    def upload(self, tag, path):
        assert tag == TAG and self.release["draft"] and path.name not in self.assets
        if self.fail_upload and self.assets:
            raise RuntimeError("interrupted")
        asset_id = 1000 + len(self.assets)
        self.writes.append(("upload", path.name))
        self.asset_payloads[asset_id] = path.read_bytes()
        self.assets[path.name] = {"name": path.name, "id": asset_id, "state": "uploaded",
                                  "digest": "sha256:" + sha256(self.asset_payloads[asset_id]).hexdigest()}


@pytest.fixture
def repo():
    return Repository()


def test_read_only_preparation_uses_original_receipts_for_partial_reruns(repo, tmp_path):
    out = tmp_path / "out"
    hashes, evidence = release.prepare(repo, VERSION, COMMIT, out)
    assert evidence["packages"]["Windows"]["attempt"] == 1
    assert evidence["packages"]["Linux"]["artifact"] == 40
    assert set(hashes) == set(release.installer_names(VERSION).values()) | {"SHA256SUMS.txt"}
    assert (out / release.CHECKSUMS).read_bytes() == release.checksum_bytes({
        n: sha256((out / n).read_bytes()).hexdigest() for n in release.installer_names(VERSION).values()
    })
    assert not repo.writes


@pytest.mark.parametrize(("damage", "error"), [
    ("sha", "main push"), ("missing-job", "jobs"), ("red-job", "Incomplete"), ("pr", "main push"),
    ("expired", "Expired"), ("zip", "ZIP digest"), ("receipt", "different artifact"),
    ("old-job", "superseded"), ("missing-receipt", "Expected one Windows receipt"),
    ("ambiguous-receipt", "Expected one Windows receipt"), ("extra-member", "package entries"),
    ("replaced-bytes", "Installer hash mismatch"), ("package-sum", "checksum file"),
])
def test_prepare_rejects_untrusted_or_ambiguous_evidence(repo, tmp_path, damage, error):
    if damage == "sha":
        repo.runs["ci.yml"]["head_sha"] = "b" * 40
    elif damage == "missing-job":
        repo.jobs[11].pop()
    elif damage == "red-job":
        repo.jobs[10][0]["conclusion"] = "cancelled"
    elif damage == "pr":
        repo.runs["desktop.yml"]["event"] = "pull_request"
    elif damage == "expired":
        repo.artifacts[31]["expired"] = True
    elif damage == "zip":
        repo.payloads[31] += b"changed"
    elif damage == "receipt":
        repo.receipts["Windows"]["artifact_id"] = 40
        repo.add(31, "desktop-receipt-Windows-1", packed({"receipt.json": json.dumps(repo.receipts["Windows"])}))
    elif damage == "old-job":
        for job in repo.original_jobs:
            job["started_at"] = "2026-10-31T15:00:00Z"
    elif damage == "missing-receipt":
        del repo.artifacts[31]
    elif damage == "ambiguous-receipt":
        repo.add(32, "desktop-receipt-Windows-2", repo.payloads[31])
    else:
        receipt = repo.receipts["Windows"]
        name = receipt["installer_name"]
        entries = {name: b"changed" if damage == "replaced-bytes" else b"Windows test installer bytes",
                   release.CHECKSUMS: release.checksum_bytes({name: receipt["installer_sha256"]})}
        if damage == "extra-member":
            entries["../skip"] = b"unexpected"
        if damage == "package-sum":
            entries[release.CHECKSUMS] = b"incorrect"
        repo.add(30, "electron-installer-Windows", packed(entries))
        receipt["artifact_digest"] = repo.artifacts[30]["digest"].split(":")[1]
        repo.add(31, "desktop-receipt-Windows-1", packed({"receipt.json": json.dumps(receipt)}))
    with pytest.raises(release.ReleaseError, match=error):
        release.prepare(repo, VERSION, COMMIT, tmp_path / "out")
    assert not repo.writes


def test_retry_reads_actual_draft_bytes_and_does_not_replace_public_assets(repo, tmp_path):
    src = tmp_path / "src"
    hashes, evidence = release.prepare(repo, VERSION, COMMIT, src)
    repo.fail_upload = True
    with pytest.raises(RuntimeError, match="interrupted"):
        release.ensure_draft(repo, VERSION, COMMIT, hashes, evidence, src)
    assert len(repo.assets) == 1 and repo.release["draft"]
    repo.fail_upload = False
    draft = release.ensure_draft(repo, VERSION, COMMIT, hashes, evidence, src)
    assert sum(method == "upload" for method, _ in repo.writes) == 3
    readback = tmp_path / "readback"
    # GitHub may retain a branch name here even though the existing tag is fixed.
    draft["target_commitish"] = "main"
    ids = release.read_draft_bytes(repo, draft, COMMIT, hashes, readback)
    assert set(ids) == set(hashes)
    payload = packed({name: (readback / name).read_bytes() for name in hashes})
    repo.add(99, "draft-readback-80-11-1", payload)
    for platform, installer in release.installer_names(VERSION).items():
        target = tmp_path / platform
        release.unpack(repo, VERSION, COMMIT, 80, hashes, platform, target, 99, sha256(payload).hexdigest(), 11, 1)
        assert {p.name for p in target.iterdir()} == {installer, release.CHECKSUMS}
        assert release.digest_file(target / installer) == hashes[installer]
    release.publish(repo, VERSION, COMMIT, 80, hashes, evidence, ids)
    writes = repo.writes.copy()
    assert not release.ensure_draft(repo, VERSION, COMMIT, hashes, evidence, src)["draft"]
    release.publish(repo, VERSION, COMMIT, 80, hashes, evidence, ids)
    assert repo.writes == writes


def test_draft_command_resumes_existing_assets_and_emits_verified_ids(repo, tmp_path, monkeypatch):
    packages = tmp_path / "packages"
    hashes, evidence = release.prepare(repo, VERSION, COMMIT, packages)
    draft = release.ensure_draft(repo, VERSION, COMMIT, hashes, evidence, packages)
    writes = repo.writes.copy()
    readback = tmp_path / "readback"
    outputs = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("GITHUB_REPOSITORY", repo.repo)
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    monkeypatch.setattr(release, "GitHub", lambda slug: repo)
    monkeypatch.setattr(sys, "argv", [
        "desktop_release.py", "draft", "--version", VERSION, "--commit", COMMIT,
        "--manifest", json.dumps(hashes), "--evidence", json.dumps(evidence),
        "--out", str(packages), "--readback", str(readback),
    ])
    release.main()
    values = dict(line.split("=", 1) for line in outputs.read_text().splitlines())
    assert values["release_id"] == str(draft["id"]) and values["already_public"] == "false"
    assert json.loads(values["asset_ids"]) == {name: item["id"] for name, item in repo.assets.items()}
    assert {file.name: release.digest_file(file) for file in readback.iterdir()} == hashes
    assert repo.writes == writes


def test_draft_transfer_pins_source_run_attempt_and_validates_all_three_files(repo, tmp_path):
    src = tmp_path / "src"
    hashes, _ = release.prepare(repo, VERSION, COMMIT, src)
    files = {name: (src / name).read_bytes() for name in hashes}
    for changed in (False, True):
        if changed:
            files[release.installer_names(VERSION)["Linux"]] = b"wrong body in verified ZIP"
        bundle = packed(files)
        repo.add(98, "release-input-11-1", bundle)
        dest = tmp_path / f"transfer-{changed}"
        args = (repo, VERSION, COMMIT, None, hashes, None, dest, 98, sha256(bundle).hexdigest(), 11, 1)
        if changed:
            with pytest.raises(release.ReleaseError, match="Transferred file hash mismatch"):
                release.unpack(*args)
        else:
            release.unpack(*args)
            assert {p.name: release.digest_file(p) for p in dest.iterdir()} == hashes
    assert not repo.writes


@pytest.mark.parametrize(("damage", "error"), [
    ("id", "assets changed"), ("notes", "notes changed"), ("digest", "differs"),
    ("extra", "Unexpected or duplicate"), ("ci", "Source CI changed"), ("tag", "different object"),
])
def test_changed_draft_or_source_blocks_publication(repo, tmp_path, damage, error):
    src = tmp_path / "src"
    hashes, evidence = release.prepare(repo, VERSION, COMMIT, src)
    draft = release.ensure_draft(repo, VERSION, COMMIT, hashes, evidence, src)
    ids = release.read_draft_bytes(repo, draft, COMMIT, hashes, tmp_path / "readback")
    if damage == "id":
        repo.assets[release.CHECKSUMS]["id"] += 1
    elif damage == "digest":
        repo.assets[release.CHECKSUMS]["digest"] = "sha256:" + "c" * 64
    elif damage == "extra":
        repo.assets["extra"] = {"name": "extra"}
    elif damage == "tag":
        repo.ref = "c" * 40
    elif damage == "notes":
        repo.release["body"] += "edited"
    else:
        repo.runs["ci.yml"]["run_attempt"] += 1
    with pytest.raises(release.ReleaseError, match=error):
        release.publish(repo, VERSION, COMMIT, 80, hashes, evidence, ids)
    assert repo.release["draft"] and ("PATCH", "releases/80") not in repo.writes


def test_declared_digest_is_insufficient_if_the_draft_returns_other_bytes(repo, tmp_path):
    src = tmp_path / "src"
    hashes, evidence = release.prepare(repo, VERSION, COMMIT, src)
    draft = release.ensure_draft(repo, VERSION, COMMIT, hashes, evidence, src)
    repo.asset_payloads[repo.assets[release.CHECKSUMS]["id"]] = b"wrong"
    with pytest.raises(release.ReleaseError, match="Downloaded draft hash mismatch"):
        release.read_draft_bytes(repo, draft, COMMIT, hashes, tmp_path / "readback")
    assert repo.release["draft"]


def test_public_verifier_streams_canonical_urls_and_retries_transient_404(repo, tmp_path):
    src = tmp_path / "src"
    hashes, _ = release.prepare(repo, VERSION, COMMIT, src)
    requests = []
    delays = []

    class Response(BytesIO):
        status = 200

    class PublicOpener:
        def open(self, url, timeout):
            requests.append(url)
            assert timeout > 0 and url.startswith(f"https://github.com/owner/project/releases/download/{TAG}/")
            if len(requests) == 1:
                raise HTTPError(url, 404, "propagating", {}, None)
            return Response((src / url.split("/")[-1]).read_bytes())

    release.verify_public(repo.repo, VERSION, COMMIT, hashes, opener=PublicOpener(), sleep=delays.append)
    assert len(requests) == 4 and len(delays) == 1
    assert {url.rsplit("/", 1)[1] for url in requests} == set(hashes)
    (src / release.installer_names(VERSION)["Linux"]).write_bytes(b"changed")
    with pytest.raises(release.ReleaseError, match="Public download hash mismatch"):
        release.verify_public(repo.repo, VERSION, COMMIT, hashes, opener=PublicOpener(), sleep=delays.append)
    assert not repo.writes


def test_public_download_rejects_unexpected_redirect():
    request = Request("https://github.com/example/asset")
    handler = release.PublicRedirect()
    redirected = handler.redirect_request(request, None, 302, "", {}, "https://release-assets.githubusercontent.com/asset")
    assert not redirected.has_header("Authorization")
    with pytest.raises(release.ReleaseError, match="Unexpected GitHub"):
        handler.redirect_request(request, None, 302, "", {}, "https://outside.example/asset")

@pytest.mark.parametrize(("path", "headers"), [
    ("actions/artifacts/14932212147/zip", []),
    ("releases/assets/12345", ["-H", "Accept: application/octet-stream"]),
])
def test_github_requests_the_correct_binary_representation(tmp_path, monkeypatch, path, headers):
    api = release.GitHub("owner/repo")
    def request(command, **kwargs):
        assert command == ["gh", "api", "repos/owner/repo/" + path, *headers]
        kwargs["stdout"].write(b"package bytes")
    monkeypatch.setattr(release.subprocess, "run", request)
    dest = tmp_path / "download"
    api.download(path, dest)
    assert dest.read_bytes() == b"package bytes"
