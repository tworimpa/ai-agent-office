"""Promote the two tested Electron installers. `prepare` only reads GitHub."""

import argparse
from base64 import b64decode
from collections import Counter
from datetime import datetime
from hashlib import file_digest, sha256
from http.client import IncompleteRead
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener
from zipfile import ZipFile


BACKGROUND = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CHECKSUMS = "SHA256SUMS.txt"
PLATFORMS = {"Windows": "windows-latest", "Linux": "ubuntu-latest"}
WORKFLOWS = {
    "ci.yml": {f"{os_name} / Python {version}" for os_name in PLATFORMS.values() for version in ("3.11", "3.13")},
    "desktop.yml": {f"{os_name} desktop installer" for os_name in PLATFORMS.values()}
    | {f"rust-comparison / {os_name} / Rust comparison" for os_name in PLATFORMS.values()},
}


class ReleaseError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ReleaseError(message)


def validate_inputs(version, commit):
    require(re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version), "Expected x.y.z version")
    require(re.fullmatch(r"[a-f0-9]{40}", commit), "Expected full lowercase commit SHA")


def installer_names(version):
    return {"Windows": f"AI-Agent-Office-Setup-{version}.exe", "Linux": f"ai-agent-office-desktop_{version}_amd64.deb"}


def checksum_bytes(hashes):
    return "".join(f"{hashes[name]}  {name}\n" for name in sorted(hashes)).encode("ascii")


def validate_manifest(hashes, version):
    require(isinstance(hashes, dict) and set(hashes) == {*installer_names(version).values(), CHECKSUMS}, "Unexpected release assets")
    for digest in hashes.values():
        require(isinstance(digest, str) and re.fullmatch("[a-f0-9]{64}", digest), "Invalid SHA-256")
    checksums = checksum_bytes({name: value for name, value in hashes.items() if name != CHECKSUMS})
    require(sha256(checksums).hexdigest() == hashes[CHECKSUMS], "Noncanonical public checksum list")


def digest_file(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


class GitHub:
    """Use gh's configured authentication and binary-safe downloads on both OSes."""

    def __init__(self, repo):
        require(re.fullmatch(r"[\w.-]+/[\w.-]+", repo), "Invalid GitHub repository")
        self.repo = repo

    def command(self, path):
        return ["gh", "api", f"repos/{self.repo}/{path}"]

    def get(self, path):
        return json.loads(subprocess.check_output(self.command(path), timeout=60, creationflags=BACKGROUND))

    def items(self, path, key=None):
        pages = json.loads(subprocess.check_output([*self.command(path), "--paginate", "--slurp"], timeout=120, creationflags=BACKGROUND))
        return [item for page in pages for item in (page[key] if key else page)]

    def download(self, path, dest):
        with dest.open("wb") as stream:
            subprocess.run([*self.command(path), "-H", "Accept: application/octet-stream"], stdout=stream, check=True, timeout=300, creationflags=BACKGROUND)

    def verified_artifact(self, artifact_id, **expected):
        from desktop_artifact import verified_artifact
        return verified_artifact(self.repo, artifact_id, **expected)

    def write(self, method, path, body):
        output = subprocess.check_output([*self.command(path), "-X", method, "--input", "-"], input=json.dumps(body).encode(),
                                         timeout=60, creationflags=BACKGROUND)
        return json.loads(output)

    def upload(self, tag, path):
        # gh defaults to rejecting a same-named asset, including on retry.
        subprocess.run(["gh", "release", "upload", tag, str(path), "--repo", self.repo], check=True,
                       timeout=300, creationflags=BACKGROUND)


def green_workflows(api, commit):
    evidence = {}
    for filename, expected in WORKFLOWS.items():
        runs = api.items(f"actions/workflows/{filename}/runs?event=push&branch=main&head_sha={commit}&per_page=100", "workflow_runs")
        require(len(runs) == 1, f"Expected one main push run for {filename} at {commit}, found {len(runs)}")
        run = runs[0]
        require(run["path"] == f".github/workflows/{filename}" and run["event"] == "push"
                and run["head_branch"] == "main" and run["head_sha"] == commit
                and all(run[key]["full_name"] == api.repo for key in ("repository", "head_repository"))
                and run["status"] == "completed" and run["conclusion"] == "success",
                f"Unsuccessful or unexpected main push run for {filename}")
        run_id = run["id"]
        jobs = api.items(f"actions/runs/{run_id}/jobs?filter=latest&per_page=100", "jobs")
        require(Counter(job["name"] for job in jobs) == Counter(expected), f"Unexpected or duplicate jobs for {filename}")
        for job in jobs:
            require(job["status"] == "completed" and job["conclusion"] == "success" and job["head_sha"] == commit
                    and job["run_id"] == run_id and 1 <= job["run_attempt"] <= run["run_attempt"],
                    f"Incomplete or mismatched job {job['name']}")
        evidence[filename] = {"id": run_id, "attempt": run["run_attempt"],
                              "jobs": {job["name"]: {"id": job["id"], "attempt": job["run_attempt"],
                                                      "started_at": job["started_at"], "completed_at": job["completed_at"]}
                                       for job in jobs}}
    return evidence


def read_archive(api, item, name, run_id, commit, dest):
    source = item["workflow_run"]
    require(item["name"] == name and not item["expired"] and source["id"] == run_id and source["head_sha"] == commit
            and source["head_branch"] == "main" and source["repository_id"] == source["head_repository_id"],
            f"Expired or unrelated artifact {name}")
    api.download(f"actions/artifacts/{item['id']}/zip", dest)
    require(item["digest"] == "sha256:" + digest_file(dest), f"Artifact ZIP digest mismatch for {name}")


def job_created(item, job):
    created = datetime.fromisoformat(item["created_at"])
    return datetime.fromisoformat(job["started_at"]) <= created <= datetime.fromisoformat(job["completed_at"])


def receipt_for_job(api, artifacts, desktop_run, platform):
    # GitHub may copy a successful job to a later attempt with a new job ID
    # and run_attempt but the ORIGINAL execution times. Its uploads stay put.
    job_name = f"{PLATFORMS[platform]} desktop installer"
    job = desktop_run["jobs"][job_name]
    candidates = []
    for item in artifacts:
        match = re.fullmatch(rf"desktop-receipt-{platform}-([1-9][0-9]*)", item["name"])
        if match and int(match[1]) <= job["attempt"] and job_created(item, job):
            candidates.append((item, int(match[1])))
    require(len(candidates) == 1, f"Expected one {platform} receipt from the successful job, found {len(candidates)}")
    item, attempt = candidates[0]
    if attempt != job["attempt"]:
        original_jobs = api.items(f"actions/runs/{desktop_run['id']}/attempts/{attempt}/jobs?per_page=100", "jobs")
        original = [j for j in original_jobs if j["name"] == job_name]
        require(len(original) == 1 and original[0]["conclusion"] == "success" and original[0]["run_attempt"] == attempt
                and all(original[0][key] == job[key] for key in ("started_at", "completed_at")),
                f"{platform} receipt belongs to a superseded job")
    return item, attempt, job


def stage_installer(api, artifacts, desktop_run, commit, version, platform, scratch, out):
    run_id = desktop_run["id"]
    receipt_meta, attempt, job = receipt_for_job(api, artifacts, desktop_run, platform)
    receipt_zip = scratch / "receipt.zip"
    read_archive(api, receipt_meta, f"desktop-receipt-{platform}-{attempt}", run_id, commit, receipt_zip)
    with ZipFile(receipt_zip) as archive:
        require(archive.namelist() == ["receipt.json"] and archive.getinfo("receipt.json").file_size <= 16384,
                f"Unexpected {platform} receipt archive entries")
        receipt = json.loads(archive.read("receipt.json"))
    public_name = installer_names(version)[platform]
    native_name = f"AI Agent Office Setup {version}.exe" if platform == "Windows" else public_name
    expected = {"schema_version": 1, "platform": platform, "version": version, "head_sha": commit,
                "run_id": run_id, "run_attempt": attempt, "installer_name": native_name}
    require(all(receipt.get(k) == v and type(receipt[k]) is type(v) for k, v in expected.items()),
            f"Unexpected {platform} receipt identity")
    package_zip = scratch / "package.zip"
    require(type(receipt["artifact_id"]) is int and receipt["artifact_id"] > 0, "Invalid installer artifact ID")
    package = api.get(f"actions/artifacts/{receipt['artifact_id']}")
    require(receipt["artifact_id"] == package["id"] and package["digest"] == "sha256:" + receipt["artifact_digest"]
            and job_created(package, job),
            f"{platform} receipt references a different artifact")
    read_archive(api, package, f"electron-installer-{platform}", run_id, commit, package_zip)
    digest = receipt["installer_sha256"]
    require(isinstance(digest, str) and re.fullmatch("[a-f0-9]{64}", digest), f"Invalid {platform} package hash")
    with ZipFile(package_zip) as archive:
        require(sorted(archive.namelist()) == sorted([native_name, CHECKSUMS]), f"Unexpected {platform} package entries")
        checksum = checksum_bytes({native_name: digest})
        require(archive.getinfo(CHECKSUMS).file_size == len(checksum) and archive.read(CHECKSUMS) == checksum,
                f"Native {platform} checksum file differs from receipt")
        with archive.open(native_name) as source, (out / public_name).open("xb") as dest:
            shutil.copyfileobj(source, dest)
    require(digest_file(out / public_name) == digest, f"Installer hash mismatch for {platform}")
    return {"name": public_name, "sha256": digest, "receipt": receipt_meta["id"], "artifact": package["id"],
            "artifact_digest": package["digest"], "attempt": attempt}


def prepare(api, version, commit, out):
    validate_inputs(version, commit)
    package = api.get(f"contents/desktop/package.json?ref={commit}")
    require(json.loads(b64decode(package["content"]))["version"] == version, "Version differs from desktop/package.json")
    compare = api.get(f"compare/{commit}...main?per_page=1")
    require(compare["status"] in ("identical", "ahead"), "Commit is outside main history")
    workflows = green_workflows(api, commit)
    desktop = workflows["desktop.yml"]
    artifacts = api.items(f"actions/runs/{desktop['id']}/artifacts?per_page=100", "artifacts")
    out.mkdir(parents=True, exist_ok=False)
    packages = {}
    with tempfile.TemporaryDirectory() as scratch:
        for platform in PLATFORMS:
            packages[platform] = stage_installer(api, artifacts, desktop, commit, version, platform, Path(scratch), out)
    hashes = {item["name"]: item["sha256"] for item in packages.values()}
    (out / CHECKSUMS).write_bytes(checksum_bytes(hashes))
    hashes[CHECKSUMS] = digest_file(out / CHECKSUMS)
    return hashes, {"workflows": workflows, "packages": packages}


def release_notes(api, version, commit, evidence):
    url = f"https://github.com/{api.repo}"
    base = f"{url}/releases/download/v{version}"
    names = installer_names(version)
    lines = [f"[Windows x64 설치 파일 (.exe)]({base}/{names['Windows']}) · "
             f"[Ubuntu amd64 설치 파일 (.deb)]({base}/{names['Linux']}) · "
             f"[SHA-256 체크섬]({base}/{CHECKSUMS})", "",
             "Windows 설치 파일은 코드 서명 없이 배포됩니다. 다운로드한 파일의 SHA-256을 체크섬 목록과 대조하세요.",
             "Windows에서는 설치 파일을 실행하고, Ubuntu에서는 .deb를 패키지 관리자로 설치하세요. "
             "업데이트 전 앱 메뉴에서 로컬 작업실을 종료하세요.",
             "로컬 Studio용 Python은 설치 파일에 포함되어 있습니다. 작업을 실행할 호스트에는 Git과 "
             "사용할 AI CLI를 설치하고 각 공급자 계정에 로그인하세요.",
             "", f"[설치·업데이트 안내]({url}/blob/{commit}/docs/desktop.md) · [소스 커밋]({url}/commit/{commit})",
             "", "### 빌드 검증", ""]
    for workflow, data in evidence["workflows"].items():
        lines.append(f"- [{workflow} 실행 {data['id']} · 시도 {data['attempt']}]({url}/actions/runs/{data['id']}/attempts/{data['attempt']})")
    for platform, data in evidence["packages"].items():
        lines.append(f"- {platform} · 시도 {data['attempt']} · 설치 파일 아티팩트 `{data['artifact']}` · "
                     f"영수증 `{data['receipt']}` · ZIP `{data['artifact_digest']}`")
    return "\n".join(lines)


def tag_exists(api, tag, commit):
    refs = [ref for ref in api.items(f"git/matching-refs/tags/{tag}") if ref["ref"] == f"refs/tags/{tag}"]
    if not refs:
        return False
    require(len(refs) == 1, "Ambiguous release tag")
    obj = refs[0]["object"]
    for _ in range(8):
        if obj["type"] != "tag":
            break
        obj = api.get(f"git/tags/{obj['sha']}")["object"]
    require(obj["type"] == "commit" and obj["sha"] == commit, "Release tag points to a different object")
    return True


def remote_assets(api, release_id, hashes, *, complete):
    assets = api.items(f"releases/{release_id}/assets?per_page=100")
    mapped = {item["name"]: item for item in assets}
    require(len(assets) == len(mapped) and set(mapped) <= set(hashes), "Unexpected or duplicate release assets")
    require(not complete or set(mapped) == set(hashes), "Missing release assets")
    for name, item in mapped.items():
        require(item["state"] == "uploaded" and item["digest"] == "sha256:" + hashes[name],
                f"Existing release asset differs or is incomplete: {name}")
    return mapped


def check_release(api, release, tag, commit, release_id, *, draft=None):
    # GitHub documents target_commitish as unused for a pre-existing tag.
    # Resolve the actual Git ref every time we are about to trust the release.
    require(release["id"] == release_id and release["tag_name"] == tag and tag_exists(api, tag, commit)
            and not release["prerelease"], "Unexpected release identity")
    if draft is not None:
        require(release["draft"] == draft, "Unexpected release publication state")


def ensure_draft(api, version, commit, hashes, evidence, directory):
    validate_inputs(version, commit)
    validate_manifest(hashes, version)
    require({p.name for p in directory.iterdir()} == set(hashes), "Unexpected staged files")
    for name, digest in hashes.items():
        require(digest_file(directory / name) == digest, f"Staged file hash mismatch: {name}")
    tag = f"v{version}"
    notes = release_notes(api, version, commit, evidence)
    releases = [release for release in api.items("releases?per_page=100") if release["tag_name"] == tag]
    require(len(releases) <= 1, "Multiple releases for the same tag")
    exists = tag_exists(api, tag, commit)
    if releases:
        release = releases[0]
        check_release(api, release, tag, commit, release["id"])
        require(exists and release["body"].replace("\r\n", "\n") == notes, "Release tag or provenance notes differ")
    else:
        if not exists:
            api.write("POST", "git/refs", {"ref": f"refs/tags/{tag}", "sha": commit})
        require(tag_exists(api, tag, commit), "Release tag was not created")
        release = api.write("POST", "releases", {"tag_name": tag, "target_commitish": commit, "name": f"AI Agent Office {version}",
                                                 "body": notes, "draft": True, "prerelease": False})
        check_release(api, release, tag, commit, release["id"], draft=True)
    release_id = release["id"]
    assets = remote_assets(api, release_id, hashes, complete=not release["draft"])
    for name in sorted(set(hashes) - set(assets)):
        check_release(api, api.get(f"releases/{release_id}"), tag, commit, release_id, draft=True)
        api.upload(tag, directory / name)
    remote_assets(api, release_id, hashes, complete=True)
    return release


def read_draft_bytes(api, release, commit, hashes, out):
    release_id = release["id"]
    check_release(api, api.get(f"releases/{release_id}"), release["tag_name"], commit, release_id, draft=True)
    assets = remote_assets(api, release_id, hashes, complete=True)
    out.mkdir(parents=True, exist_ok=False)
    for name in hashes:
        api.download(f"releases/assets/{assets[name]['id']}", out / name)
        require(digest_file(out / name) == hashes[name], f"Downloaded draft hash mismatch for {name}")
    return {name: item["id"] for name, item in assets.items()}


def unpack(api, version, commit, release_id, hashes, platform, out, artifact_id, artifact_digest, current_run, artifact_attempt):
    validate_inputs(version, commit)
    validate_manifest(hashes, version)
    require(artifact_attempt > 0, "Invalid transfer artifact attempt")
    require((release_id is None) == (platform is None), "Specify both platform and release ID for draft readback")
    if release_id is None:
        prefix = "release-input"
        selected = sorted(hashes)
    else:
        require(release_id > 0, "Invalid release ID")
        prefix = f"draft-readback-{release_id}"
        selected = (installer_names(version)[platform], CHECKSUMS)
    with api.verified_artifact(artifact_id, run_id=current_run, name=f"{prefix}-{current_run}-{artifact_attempt}",
                               digest=artifact_digest, filenames=tuple(hashes)) as archive:
        out.mkdir(parents=True, exist_ok=False)
        for name in selected:
            with archive.open(name) as source, (out / name).open("xb") as dest:
                shutil.copyfileobj(source, dest)
            require(digest_file(out / name) == hashes[name], f"Transferred file hash mismatch for {name}")


def publish(api, version, commit, release_id, hashes, evidence, asset_ids):
    validate_inputs(version, commit)
    validate_manifest(hashes, version)
    tag = f"v{version}"
    require(tag_exists(api, tag, commit), "Missing release tag")
    release = api.get(f"releases/{release_id}")
    check_release(api, release, tag, commit, release_id)
    require(release["body"].replace("\r\n", "\n") == release_notes(api, version, commit, evidence), "Release notes changed")
    require(green_workflows(api, commit) == evidence["workflows"], "Source CI changed since package verification")
    assets = remote_assets(api, release_id, hashes, complete=True)
    require({name: item["id"] for name, item in assets.items()} == asset_ids, "Draft assets changed since native verification")
    if release["draft"]:
        api.write("PATCH", f"releases/{release_id}", {"draft": False})
    check_release(api, api.get(f"releases/{release_id}"), tag, commit, release_id, draft=False)
    remote_assets(api, release_id, hashes, complete=True)
    require(tag_exists(api, tag, commit), "Published tag missing")


class PublicRedirect(HTTPRedirectHandler):
    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        require(target.scheme == "https" and not target.username and not target.password
                and target.hostname in ("github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"),
                "Unexpected GitHub release download redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def verify_public(repo, version, commit, hashes, *, opener=None, sleep=time.sleep):
    validate_inputs(version, commit)
    validate_manifest(hashes, version)
    # urllib sends no CLI or Actions token and doesn't consult .netrc.
    opener = opener or build_opener(PublicRedirect())
    base = f"https://github.com/{repo}/releases/download/v{version}"
    for name, expected in hashes.items():
        url = f"{base}/{name}"
        for attempt in range(5):
            try:
                with opener.open(url, timeout=60) as stream:
                    require(stream.status == 200, f"Public download failed for {name}")
                    digest = file_digest(stream, "sha256").hexdigest()
                require(digest == expected, f"Public download hash mismatch for {name}")
                print(f"{expected}  {url}")
                break
            except HTTPError as exc:
                if exc.code not in (404, 408, 429, 500, 502, 503, 504) or attempt == 4:
                    raise
            except (URLError, TimeoutError, ConnectionError, IncompleteRead):
                if attempt == 4:
                    raise
            sleep(2 ** (attempt + 1))


def output(key, value):
    serialized = json.dumps(value, separators=(",", ":")) if isinstance(value, dict) else str(value)
    print(f"{key}={serialized}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as stream:
            print(f"{key}={serialized}", file=stream)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"))
    sub = parser.add_subparsers(dest="cmd", required=True)
    for command in ("prepare", "draft", "unpack", "publish", "verify-public"):
        p = sub.add_parser(command)
        p.add_argument("--commit", required=True)
        if command == "unpack":
            p.add_argument("--tag", required=True)
            p.add_argument("--platform", choices=PLATFORMS)
            p.add_argument("--artifact-id", type=int, required=True)
            p.add_argument("--artifact-digest", required=True)
            p.add_argument("--artifact-attempt", type=int, required=True)
        else:
            p.add_argument("--version", required=True)
        if command != "prepare":
            p.add_argument("--manifest", type=json.loads, required=True)
        if command in ("prepare", "draft", "unpack"):
            p.add_argument("--out", type=Path, required=True)
        if command in ("draft", "publish"):
            p.add_argument("--evidence", type=json.loads, required=True)
        if command in ("unpack", "publish"):
            p.add_argument("--release-id", type=int, required=command == "publish")
        if command == "draft":
            p.add_argument("--readback", type=Path, required=True)
        if command == "publish":
            p.add_argument("--asset-ids", type=json.loads, required=True)
    args = parser.parse_args()
    require(args.repo, "Specify --repo owner/repository")
    if args.cmd in ("draft", "publish"):
        require(os.environ.get("GITHUB_ACTIONS") == "true" and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
                and os.environ.get("GITHUB_REF") == "refs/heads/main" and os.environ.get("GITHUB_REPOSITORY") == args.repo,
                "Release writes require workflow_dispatch from the repository's main branch")
    api = GitHub(args.repo)
    if args.cmd == "prepare":
        hashes, evidence = prepare(api, args.version, args.commit, args.out)
        output("manifest", hashes)
        output("evidence", evidence)
    elif args.cmd == "unpack":
        require(args.tag.startswith("v"), "Expected vX.Y.Z tag")
        unpack(api, args.tag[1:], args.commit, args.release_id, args.manifest, args.platform, args.out,
               args.artifact_id, args.artifact_digest, int(os.environ["GITHUB_RUN_ID"]), args.artifact_attempt)
    elif args.cmd == "draft":
        release = ensure_draft(api, args.version, args.commit, args.manifest, args.evidence, args.out)
        output("release_id", release["id"])
        output("already_public", str(not release["draft"]).lower())
        if release["draft"]:
            output("asset_ids", read_draft_bytes(api, release, commit, args.manifest, args.readback))
    elif args.cmd == "publish":
        publish(api, args.version, args.commit, args.release_id, args.manifest, args.evidence, args.asset_ids)
    else:
        verify_public(api.repo, args.version, args.commit, args.manifest)


if __name__ == "__main__":
    main()
