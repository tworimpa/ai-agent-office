"""Installed payload and separately identified shared prerequisites, in logical file bytes."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


def files(paths):
    seen = set()
    total = 0
    for path in paths:
        if path.is_symlink():
            continue
        if path.is_file():
            stat = path.stat()
            key = (stat.st_dev, stat.st_ino)
            if key not in seen:
                seen.add(key)
                total += stat.st_size
    return dict(files=len(seen), bytes=total)


def tree(path):
    return files(path.rglob("*") if path.is_dir() else [path])


def debian(package):
    paths = subprocess.check_output(["dpkg-query", "-L", package], text=True).splitlines()
    info = subprocess.check_output(["dpkg-query", "-W", "-f=${Version}\n${Depends}", package], text=True).splitlines()
    return dict(name=package, version=info[0], depends=info[1] if len(info) > 1 else "",
                **files(Path(p) for p in paths if p.startswith("/")))


def apt_estimate(package, *, minimum):
    import apt
    env = {**os.environ, "LC_ALL": "C"}
    flags = ["--no-install-recommends"] if minimum else []
    plan = subprocess.check_output(["apt-get", "-s", *flags, "-o", "Debug::NoLocking=true",
                                    "install", str(package.resolve(strict=True))], text=True, env=env)
    name = subprocess.check_output(["dpkg-deb", "-f", str(package), "Package"], text=True).strip()
    cache = apt.Cache()
    dependencies = []
    for package_id, version in re.findall(r"^Inst (\S+)(?: \[[^\]]+\])? \((\S+)", plan, re.MULTILINE):
        if package_id == name:
            continue
        dep = cache[package_id]
        candidate = dep.versions[version]
        previous = dep.installed.installed_size if dep.installed else 0
        dependencies.append(dict(package=package_id, version=version, downloadBytes=candidate.size,
                                 additionalInstalledBytes=candidate.installed_size - previous))
    return dict(package=name, method=" ".join(["apt-get", "-s", *flags]),
                priorAppVersion=cache[name].installed.version if name in cache and cache[name].installed else None,
                additionalDependencyBytes=sum(d["additionalInstalledBytes"] for d in dependencies),
                dependencyDownloadBytes=sum(d["downloadBytes"] for d in dependencies), dependencies=dependencies)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--installed-dir", type=Path)
    target.add_argument("--installed-deb")
    target.add_argument("--estimate-apt-only", action="store_true")
    parser.add_argument("--studio-runtime", type=Path)
    parser.add_argument("--includes-studio", action="store_true")
    parser.add_argument("--shared-dir", type=Path, action="append", default=[])
    parser.add_argument("--shared-deb", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.estimate_apt_only:
        with args.package.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        estimates = {name: apt_estimate(args.package, minimum=minimum)
                     for name, minimum in (("defaultApt", False), ("requiredOnly", True))}
        with args.output.open("x", encoding="utf-8") as output:
            json.dump(dict(sha256=digest, **estimates), output, indent=2)
        return
    if args.studio_runtime is None:
        parser.error("--studio-runtime is required for installed payloads")
    for path in [args.package, args.studio_runtime, *args.shared_dir]:
        path.resolve(strict=True)
    if args.installed_dir:
        args.installed_dir.resolve(strict=True)
    installed = tree(args.installed_dir) if args.installed_dir else debian(args.installed_deb)
    runtime = tree(args.studio_runtime)
    shared = {p.name: tree(p) for p in args.shared_dir}
    shared.update({p: debian(p) for p in args.shared_deb})
    with args.package.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    info = dict(artifact=args.package.name, packageBytes=args.package.stat().st_size, sha256=digest,
                installedPayload=installed, commonStudioRuntime=runtime, studioBundled=args.includes_studio,
                payloadWithStudioBytes=installed["bytes"] + (0 if args.includes_studio else runtime["bytes"]),
                sharedRendererComponents=shared,
                byteDefinition="Regular files, hardlinks once per inventory, excluding symlinks and directory metadata.")
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(info, output, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
