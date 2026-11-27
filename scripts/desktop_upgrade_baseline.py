"""Recover a verified historical or public installer for upgrade checks."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import zipfile

from desktop_release import CHECKSUMS, GitHub, digest_file, installer_names, remote_assets, tag_exists, validate_manifest

REPOSITORY = "tworimpa/ai-agent-office"
COMMIT = "bbf4e06ffad8b66c596b4bda30aab46abf0ef99b"
RUN = 45030107146
PUBLIC_COMMIT = "ea0653faf6f595da34d66f757b0bcc2262c6fdba"
PUBLIC_RELEASE_ID = 457048919
PUBLIC_TAG = "v0.9.0"
PUBLIC_ASSET_IDS = {
    "AI-Agent-Office-Setup-0.9.0.exe": 787991736,
    "ai-agent-office-desktop_0.9.0_amd64.deb": 787992360,
    CHECKSUMS: 787992262,
}
PUBLIC_HASHES = {
    "AI-Agent-Office-Setup-0.9.0.exe": "bc833e54fdcc354d623171207e8cada55d1831236856f57dff44c33d7a9521c0",
    "ai-agent-office-desktop_0.9.0_amd64.deb": "10c4c475dcd1639779d2248f3eb53ef8db5cc130ad632a7b74f4503a203a3cd7",
    CHECKSUMS: "9f2019c8cf08d94b88432d4c9ae6e652d3ec6d535be3df19310f9f0b90e4649c",
}
# PR #37 first distributed this personal 0.8.1 release. Later main builds reused its
# version and filenames. The original successful jobs' upload logs identify these IDs.
BASELINES = {
    "Windows": {
        "job_id": 137231606062, "artifact_id": 14757755238,
        "archive_sha256": "4a40613bf570ab2eec23af9fbdd2429c2ccd80cdf44b1d34cfe5360b336a61ce",
        "installer": "AI Agent Office Setup 0.8.1.exe",
        "installer_sha256": "a149a5dabd3240c46d8c0aa98e0f2a8981c480af95ad0901b4bd2a42f2387e97",
    },
    "Linux": {
        "job_id": 137231606260, "artifact_id": 14757447261,
        "archive_sha256": "9ae328aaec4b02e1155710052b5d6a667f1ccb52bd82b83f7133bce8c3ae9bbd",
        "installer": "ai-agent-office-desktop_0.8.1_amd64.deb",
        "installer_sha256": "d4e93180f0f90fd008b896a699bb5178c7b815f3235b0e86743a02b882d58ea4",
    },
}


def gh_api(endpoint, output=subprocess.PIPE):
    result = subprocess.run(["gh", "api", f"repos/{REPOSITORY}/{endpoint}"],
                            stdout=output, stderr=subprocess.PIPE, timeout=300,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        message = result.stderr.decode(errors="replace")
        if re.search(r"\bHTTP (404|410)\b", message):
            return None
        raise RuntimeError(f"GitHub API request failed: {message.strip()}")
    return result


def archived_installer(platform, target):
    pin = BASELINES[platform]
    endpoint = f"actions/artifacts/{pin['artifact_id']}"
    result = gh_api(endpoint)
    if result is None:
        return False
    artifact = json.loads(result.stdout)
    run = artifact["workflow_run"]
    if (artifact["id"] != pin["artifact_id"] or run["id"] != RUN or run["head_sha"] != COMMIT
            or run["head_branch"] != "main" or artifact["digest"] != "sha256:" + pin["archive_sha256"]):
        raise ValueError("Historical artifact identity or digest differs from the tested main build")
    if artifact["expired"]:
        return False
    with tempfile.TemporaryFile() as archive:
        if gh_api(endpoint + "/zip", archive) is None:
            return False
        archive.seek(0)
        if hashlib.file_digest(archive, "sha256").hexdigest() != pin["archive_sha256"]:
            raise ValueError("Historical archive checksum mismatch")
        archive.seek(0)
        with zipfile.ZipFile(archive) as package:
            if package.namelist() != [pin["installer"]]:
                raise ValueError("Unexpected historical archive member")
            with package.open(pin["installer"]) as src, target.open("xb") as dst:
                shutil.copyfileobj(src, dst)
    return True


def published_baseline(api, platform, out):
    version = PUBLIC_TAG[1:]
    release = api.get(f"releases/tags/{PUBLIC_TAG}")
    if (release["id"] != PUBLIC_RELEASE_ID or release["tag_name"] != PUBLIC_TAG or release["draft"]
            or release["prerelease"] or release["target_commitish"] != PUBLIC_COMMIT
            or not tag_exists(api, PUBLIC_TAG, PUBLIC_COMMIT)):
        raise ValueError("Published 0.9.0 release identity differs")
    validate_manifest(PUBLIC_HASHES, version)
    assets = remote_assets(api, PUBLIC_RELEASE_ID, PUBLIC_HASHES, complete=True)
    if {name: asset["id"] for name, asset in assets.items()} != PUBLIC_ASSET_IDS:
        raise ValueError("Published 0.9.0 release asset IDs differ")
    name = installer_names(version)[platform]
    with tempfile.TemporaryDirectory(dir=out) as scratch:
        scratch = Path(scratch)
        checksum = scratch / CHECKSUMS
        package = scratch / name
        api.download(f"releases/assets/{PUBLIC_ASSET_IDS[CHECKSUMS]}", checksum)
        if digest_file(checksum) != PUBLIC_HASHES[CHECKSUMS]:
            raise ValueError("Published checksum list differs")
        api.download(f"releases/assets/{PUBLIC_ASSET_IDS[name]}", package)
        if digest_file(package) != PUBLIC_HASHES[name]:
            raise ValueError("Published installer checksum mismatch")
        with package.open("rb") as src, (out / name).open("xb") as dst:
            shutil.copyfileobj(src, dst)
    return {
        "repository": REPOSITORY, "commit": PUBLIC_COMMIT, "source": "published-release",
        "platform": platform, "version": version, "release_id": PUBLIC_RELEASE_ID,
        "checksums_asset_id": PUBLIC_ASSET_IDS[CHECKSUMS],
        "historical_artifact": {"installer": name, "asset_id": PUBLIC_ASSET_IDS[name]},
        "installed_sha256": PUBLIC_HASHES[name],
    }


def write_receipt(out, receipt):
    print(json.dumps(receipt))
    (out / "baseline.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=BASELINES)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--rebuilt", type=Path, help="Canonical source checkout with a finished native build")
    parser.add_argument("--version", choices=("0.8.1", "0.9.0"), default="0.8.1")
    args = parser.parse_args()
    if args.rebuilt and args.version != "0.8.1":
        parser.error("Source rebuild is available only for the historical 0.8.1 artifact")
    args.out.mkdir(parents=True, exist_ok=True)
    if args.version == "0.9.0":
        write_receipt(args.out, published_baseline(GitHub(REPOSITORY), args.platform, args.out))
        return
    pin = BASELINES[args.platform]
    target = args.out / pin["installer"]
    if args.rebuilt:
        source = args.rebuilt
        head = subprocess.check_output(["git", "-C", source, "rev-parse", "HEAD"], text=True, timeout=15,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).strip()
        version = json.loads((source / "desktop" / "package.json").read_text(encoding="utf-8"))["version"]
        if head != COMMIT or version != "0.8.1":
            raise ValueError("The fallback build must be from the pinned 0.8.1 commit")
        subprocess.run(["git", "-C", source, "diff", "--exit-code", "HEAD", "--"], check=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with (source / "desktop" / "dist" / pin["installer"]).open("rb") as src, target.open("xb") as dst:
            shutil.copyfileobj(src, dst)
        mode = "pinned-source-rebuild"
    elif archived_installer(args.platform, target):
        mode = "original-tested-archive"
    else:
        print("Original artifact expired or unavailable (HTTP 404/410). Rebuild the fixed 0.8.1 source.")
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            output.write(f"rebuild=true\nsource-commit={COMMIT}\n")
        return
    with target.open("rb") as src:
        digest = hashlib.file_digest(src, "sha256").hexdigest()
    if not args.rebuilt and digest != pin["installer_sha256"]:
        raise ValueError("Historical installer checksum mismatch")
    receipt = dict(repository=REPOSITORY, commit=COMMIT, source=mode, platform=args.platform,
                   version="0.8.1", historical_run_id=RUN, historical_artifact=pin, installed_sha256=digest)
    write_receipt(args.out, receipt)


if __name__ == "__main__":
    main()
