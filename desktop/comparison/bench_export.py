"""Export paired trials without private rig paths, host names, environment or command lines."""

import argparse
from collections import Counter
import gzip
import hashlib
import json
from pathlib import Path
import shutil

from bench_summary import summarize


def export(source, target, packages, display, apt_plans=None):
    def read(path):
        return json.loads(path.read_text(encoding="utf-8"))

    env = read(source / "environment.json")
    if env["repeats"] < 5 or env["idle"] < 9:
        raise ValueError("A release comparison needs five pairs and the full 9 second idle interval")
    rows, notes = [], Counter()
    for dataset in ("empty", "loaded"):
        for index in range(1, env["repeats"] + 1):
            for phase in ("fresh", "reopen"):
                for app in ("electron", "rust"):
                    stem = f"{dataset}-{index}-{phase}-{app}"
                    trial = read(source / (stem + ".json"))
                    viewport = trial["renderer"]
                    if (viewport["width"], viewport["height"]) != (1280, 900) or "sizingMs" not in trial:
                        raise ValueError(f"Mismatched viewport or missing resize measurement in {stem}")
                    log = (source / (stem + ".stderr")).read_bytes()
                    warnings = Counter()
                    # Publish stable diagnostic categories. Keep the complete local stderr
                    # verifiable by digest without copying tool output or absolute paths.
                    for line in log.decode("utf-8", errors="replace").splitlines():
                        if not line.strip():
                            continue
                        for pattern, label in (("DRI3", "DRI3"), ("free():", "allocator"),
                                               ("unregister class", "window_class_unregister"),
                                               ("Failed to connect to the bus", "dbus"),
                                               ("EGL", "EGL")):
                            if pattern in line:
                                warnings[label] += 1
                                break
                        else:
                            warnings["other"] += 1
                    notes.update(warnings)
                    trial["stderr"] = dict(bytes=len(log), sha256=hashlib.sha256(log).hexdigest(), counts=warnings)
                    rows.append(trial)
    if len({r["renderer"]["dpr"] for r in rows}) != 1:
        raise ValueError("The renderers used different device pixel ratios")
    target.mkdir(parents=True, exist_ok=False)
    summarize(source)
    shutil.copyfile(source / "trials.csv", target / "trials.csv")
    with (target / "raw.jsonl.gz").open("wb") as output:
        with gzip.GzipFile(fileobj=output, mode="wb", mtime=0, filename="") as compressed:
            for row in rows:
                compressed.write((json.dumps(row, separators=(",", ":")) + "\n").encode())
    public = {key: env[key] for key in ("start", "os", "python", "psutil", "logicalCPUs", "totalRAM", "repeats", "idle",
                                        "memoryMetric", "executableSha256", "studioPythonSha256", "probeSha256",
                                        "studioOfficeSha256", "studioServerSha256", "benchmarkSha256",
                                        "mockProviderSha256")}
    assets = Path(env["executable"]["electron"]).parent / "resources/app.asar"
    if assets.is_file():
        with assets.open("rb") as stream:
            public["electronAsarSha256AtExport"] = hashlib.file_digest(stream, "sha256").hexdigest()
    public.update(display=display, completedLaunches=len(rows), stderrLineCounts=notes,
                  packages={name: read(path) for name, path in zip(("electron", "rust"), packages, strict=True)})
    if apt_plans:
        for name, path in zip(("electron", "rust"), apt_plans, strict=True):
            public["packages"][name]["baseHostAptSimulation"] = read(path)
    (target / "environment.json").write_text(json.dumps(public, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    parser.add_argument("--packages", nargs=2, required=True, type=Path, metavar=("ELECTRON", "RUST"))
    parser.add_argument("--display", required=True)
    parser.add_argument("--apt-plans", nargs=2, type=Path, metavar=("ELECTRON", "RUST"))
    args = parser.parse_args()
    export(args.source, args.target, args.packages, args.display, args.apt_plans)
