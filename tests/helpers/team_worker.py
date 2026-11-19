"""Deterministic local CLI programs for the remote Studio group scenario."""
import json
import os
from pathlib import Path
import sys
import time


def run():
    engine = sys.argv[1]
    if "--version" in sys.argv or "--help" in sys.argv:
        print("1.0.0")
        return
    raw = sys.stdin.read()
    prompt = json.loads(raw)["message"]["content"] if engine == "agy" else raw
    assert "Keep the original checkout clean" in prompt
    assert "REPLACEMENT_INSTRUCTION" not in prompt
    label = next(label for label in ("web-goal", "api-goal") if label in prompt)
    directory = Path(os.environ["OFFICE_TEAM_GATE"])
    if engine == "agy":
        assert "CHECK_THE_RESULT" in prompt and "IMPLEMENT_ONE_PART" not in prompt
        print(json.dumps({"status": "SUCCESS", "response": f"VERDICT: APPROVED\nReviewed {label}"}))
        return
    assert "IMPLEMENT_ONE_PART" in prompt and "CHECK_THE_RESULT" not in prompt
    assert engine == ("claude" if label == "web-goal" else "codex")
    (directory / (label + ".started")).touch()
    limit = time.monotonic() + 50
    while not (directory / "continue").exists():
        if time.monotonic() > limit:
            raise RuntimeError("Fixture gate timed out")
        time.sleep(.05)
    area = label.split("-")[0]
    (Path(area) / "value.txt").write_text("ready" if area == "web" else "needs-fix")
    text = "Completed " + label
    if engine == "claude":
        print(json.dumps({"type": "result", "subtype": "success", "result": text, "is_error": False}))
    else:
        print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}))


if __name__ == "__main__":
    run()
