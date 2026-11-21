"""Generate auditable per-trial metrics and within-OS paired median/range comparisons."""

import argparse
import csv
import gzip
import json
from pathlib import Path
from statistics import median


def memory(rows):
    if any(p["bytes"] is None for p in rows):
        return None
    return sum(p["bytes"] for p in rows) / 2**20


def cpu(samples, group):
    total = 0
    changed = False
    for previous, current in zip(samples, samples[1:]):
        def counters(state):
            return {(p["pid"], p["birth"]): p["cpu"] for p in state["processes"][group]}
        start, end = counters(previous), counters(current)
        changed |= start.keys() != end.keys()
        total += sum(end[key] - start.get(key, 0) for key in end)
    return 100 * total / (samples[-1]["observed"] - samples[0]["observed"]), changed


def flatten(result):
    row = {k: result[k] for k in ("iteration", "dataset", "phase", "app", "readyMs")}
    if "sizingMs" in result:
        row["sizingMs"] = result["sizingMs"]
    row.update({k: v for k, v in result["actions"].items() if k.endswith("Ms")})
    states = [result[k] for k in ("before", "middle", "after")]
    for group in states[0]["processes"]:
        groups = [s["processes"][group] for s in states]
        amounts = [memory(g) for g in groups]
        row[group + "MiB"] = median(amounts) if None not in amounts else None
        row[group + "CPU"], row[group + "CPUChurn"] = cpu(states, group)
        row[group + "PIDs"] = len(groups[-1])
        row[group + "Missing"] = max(sum(p["bytes"] is None for p in g) for g in groups)
        row[group + "PostCloseMiB"] = memory(result["postClose"]["processes"][group])
    for label, names in (("officeMiB", ("shell", "studio")), ("totalMiB", ("shell", "studio", "worker", "cli", "tests"))):
        values = [memory([p for name in names for p in s["processes"][name]]) for s in states]
        row[label] = median(values) if None not in values else None
    row["postCloseSeconds"] = result["postClose"]["observed"] - result["closeAt"]
    return row


def stats(values):
    return {"n": len(values), "median": median(values), "min": min(values), "max": max(values)} if values else {"n": 0}


def summarize(root):
    rows = []
    for path in sorted(root.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if "before" in data:
            rows.append(flatten(data))
    if not rows and (root / "raw.jsonl.gz").exists():
        with gzip.open(root / "raw.jsonl.gz", "rt", encoding="utf-8") as archive:
            rows.extend(flatten(json.loads(line)) for line in archive)
    if not rows:
        raise ValueError("No completed samples")
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with (root / "trials.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fields)
        writer.writeheader()
        writer.writerows(rows)
    summary = {}
    for dataset, phase in sorted({(r["dataset"], r["phase"]) for r in rows}):
        pairs = {(r["app"], r["iteration"]): r for r in rows if (r["dataset"], r["phase"]) == (dataset, phase)}
        section = summary[dataset + "-" + phase] = {}
        for key in fields:
            if not key.endswith(("Ms", "MiB", "CPU")):
                continue
            measured = {group: {i: row[key] for (name, i), row in pairs.items() if name == group and row.get(key) is not None}
                        for group in ("electron", "rust")}
            electron, rust = measured.values()
            both = sorted(electron.keys() & rust.keys())
            section[key] = {name: stats(list(values.values())) for name, values in measured.items()}
            section[key]["pairedRustMinusElectron"] = stats([rust[i] - electron[i] for i in both])
            section[key]["pairedReductionPercent"] = stats([100 * (1 - rust[i] / electron[i]) for i in both if electron[i]])
    (root / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    summarize(parser.parse_args().directory)
