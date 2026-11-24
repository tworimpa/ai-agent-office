"""Recover the exact tested 0.8.1 installer, or identify a pinned-source rebuild."""

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


REPOSITORY = "tworimpa/ai-agent-office"
COMMIT = "bbf4e06ffad8b66c596b4bda30aab46abf0ef99b"
RUN = 45030107146
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True, choices=BASELINES)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--rebuilt", type=Path, help="Canonical source checkout with a finished native build")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
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
    print(json.dumps(receipt))
    (args.out / "baseline.json").write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
