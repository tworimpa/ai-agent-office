"""Matched, installed desktop launches against one disposable Studio per dataset."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import psutil

from bench_process import Family, native_window, snapshot


HERE = Path(__file__).resolve().parent


def sha256(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def save(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


@contextmanager
def studio(root, python, dataset):
    env = {**os.environ, "PYTHONPATH": str(HERE.parents[1] / "src"), "PYTHONUTF8": "1"}
    with subprocess.Popen([python, HERE / "bench-studio.py", root, dataset], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, env=env,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0) as process:
        try:
            line = process.stdout.readline()
            if not line:
                raise RuntimeError("Studio fixture failed to start")
            setup = json.loads(line)
            yield setup, Family(process.pid)
        finally:
            process.stdin.close()
            process.wait(timeout=60)


def await_renderer(app, events, shell):
    end = time.monotonic() + 65
    ack = events.with_suffix(".sized")
    while time.monotonic() < end and app.poll() is None:
        if events.exists():
            # Inspect only newline-terminated observations, as the Studio writes concurrently.
            text = events.read_bytes()
            lines = [json.loads(line) for line in text.split(b"\n")[:-1]]
            for row in lines:
                if row["event"] == "viewport" and not ack.exists():
                    view = row["renderer"]
                    dx = round((1280 - view["width"]) * view["dpr"])
                    dy = round((900 - view["height"]) * view["dpr"])
                    native_window(app.pid, (dx, dy))
                    ack.touch()
                if row["event"] == "error":
                    raise RuntimeError(f"Renderer action failed: {row}")
            if lines and lines[-1]["event"] == "done":
                ready = next(row for row in lines if row["event"] == "ready")
                if (ready["renderer"]["width"], ready["renderer"]["height"]) != (1280, 900):
                    raise RuntimeError("The actual viewport differs from 1280 × 900 CSS pixels")
                return ready, lines[-1]
        # Capture reparented helpers as early as possible. Keep polling out of idle CPU measurements.
        shell.processes()
        time.sleep(.05)
    raise RuntimeError(f"Renderer timeout/exit {app.poll()}. Records: {events}")


def trial(executable, name, profile, reopened, root, setup, backend, idle, stderr):
    nonce = uuid4().hex
    page = "/workspace"
    if not reopened:
        profile.mkdir()
        conn = dict(origin=setup["origin"], journalId=setup["journalId"])
        saved = {"connection": {**conn, "mode": "attached"}, "recent": {setup["journalId"]: page}, "remotes": []} if name == "electron" else {**conn, "page": page}
        save(profile / "connection.json", saved)
    observed = {k: setup.get(k) for k in ("active", "candidate", "question")}
    save(root / "trial.json", {**observed, "nonce": nonce, "reopened": reopened})
    args = ["--user-data-dir=" + str(profile)] if name == "electron" else ["--profile-dir", str(profile)]
    env = dict(os.environ)
    if sys.platform.startswith("linux"):
        env["GDK_BACKEND"] = "x11"
        if name == "electron":
            args.append("--ozone-platform=x11")
    started = time.perf_counter()
    app = subprocess.Popen([executable, *args], env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=stderr)
    shell = Family(app.pid)
    try:
        ready, action = await_renderer(app, root / (nonce + ".jsonl"), shell)
        time.sleep(1)
        before = snapshot(shell, backend, setup)
        time.sleep(idle / 2)
        middle = snapshot(shell, backend, setup)
        time.sleep(idle / 2)
        after = snapshot(shell, backend, setup)
        if len(after["processes"]["shell"]) < 2:
            raise RuntimeError("Shell census lacks its renderer")
        closed = time.perf_counter()
        native_window(app.pid)
        app.wait(timeout=12)
        if app.returncode:
            raise RuntimeError(f"Desktop close failed ({app.returncode})")
        time.sleep(max(0, closed + 3 - time.perf_counter()))
        post = snapshot(shell, backend, setup)
        if setup.get("active"):
            for state in (before, middle, after, post):
                pids = {p["pid"] for group in ("worker", "cli") for p in state["processes"][group]}
                if not {setup["worker"], setup["cli"]} <= pids:
                    raise RuntimeError("The active worker or CLI changed during the trial")
        return dict(readyMs=(ready["observed"] - started) * 1000, actions=action,
                    before=before, middle=middle, after=after, postClose=post, closeAt=closed,
                    pid=app.pid, journal=setup["journalId"], active=setup.get("active"),
                    initialRun=ready.get("initialRun"), renderer=ready["renderer"], sizingMs=ready["sizingMs"], nonce=nonce)
    finally:
        if app.poll() is None:
            try:
                native_window(app.pid)
                app.wait(timeout=6)
            except (RuntimeError, subprocess.TimeoutExpired):
                app.terminate()
                app.wait(timeout=6)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--electron", type=Path)
    parser.add_argument("--rust", type=Path)
    parser.add_argument("--studio-python", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--idle", type=float, default=9)
    parser.add_argument("--datasets", nargs="+", choices=("empty", "loaded"), default=["empty", "loaded"])
    args = parser.parse_args()
    if args.idle <= 0 or args.repeats <= 0:
        parser.error("Idle window and repetitions must be positive")
    apps = [(name, path.resolve(strict=True)) for name in ("electron", "rust") if (path := getattr(args, name))]
    if not apps:
        parser.error("Supply at least one desktop executable")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    root = Path(tempfile.mkdtemp(prefix="office-shell-benchmark-"))
    info = dict(start=datetime.now(timezone.utc).isoformat(), os=platform.platform(),
                python=sys.version, psutil=psutil.__version__, logicalCPUs=psutil.cpu_count(),
                totalRAM=psutil.virtual_memory().total, executable={name: str(path) for name, path in apps},
                studioPython=str(args.studio_python.resolve(strict=True)), repeats=args.repeats, idle=args.idle,
                executableSha256={name: sha256(path) for name, path in apps},
                studioPythonSha256=sha256(args.studio_python), probeSha256=sha256(HERE / "bench-page.js"),
                studioServerSha256=sha256(HERE.parents[1] / "src/agent_office/studio_server.py"),
                benchmarkSha256={name: sha256(HERE / name) for name in ("benchmark.py", "bench_process.py", "bench-studio.py")},
                mockProviderSha256=sha256(HERE.parent / "fixtures/goal_worker.py"),
                studioOfficeSha256=sha256(HERE.parents[1] / "src/agent_office/web/workspace-office.js"),
                memoryMetric="private_commit_bytes" if os.name == "nt" else "proportional_set_bytes",
                display=os.environ.get("DISPLAY"), scratch=str(root))
    save(output / "environment.json", info)
    for dataset in args.datasets:
        data = root / dataset
        with studio(data, args.studio_python, dataset) as (setup, backend):
            save(output / (dataset + "-studio.json"), setup)
            for iteration in range(1, args.repeats + 1):
                order = apps if iteration % 2 == 1 else list(reversed(apps))
                for reopened in (False, True):
                    for name, exe in order:
                        if (output / "STOP").exists():
                            raise SystemExit("Stopped at a trial boundary; earlier samples remain in the output directory")
                        phase = "reopen" if reopened else "fresh"
                        print(dataset, iteration, phase, name, flush=True)
                        filename = f"{dataset}-{iteration}-{phase}-{name}"
                        with (output / (filename + ".stderr")).open("wb") as stderr:
                            result = trial(exe, name, data / f"{name}-{iteration}", reopened, data,
                                           setup, backend, args.idle, stderr)
                        result.update(dataset=dataset, iteration=iteration, app=name, phase=phase)
                        save(output / (filename + ".json"), result)


if __name__ == "__main__":
    main()
