"""Own a bounded, cancellable consultation under the originating Studio worker."""
import asyncio
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_office.away_evidence import DelegationDeferred
from agent_office.studio_process import run_until_owner_closes


# Two roles with four 8 KiB instructions each, including JSON escaping, and the
# original evidence parameters. The evidence result has a separate, smaller cap.
MAX_CONSULT_INPUT = 160 * 1024
STAGE_FRAMES = {f"{role} {status}\n".encode("ascii"): (role, status)
                for role in ("specialist", "verifier") for status in ("RUNNING", "COMPLETED", "FAILED")}


def _command(mode):
    return [sys.executable, "-I", str(Path(__file__).resolve()), mode]


async def run_consultation(repo, revision, rule, specialist_engine, verifier_engine, folders, *,
                           skill_guidance=None, on_stage=None):
    from agent_office.technical_choices import CHOICE_TOPIC
    if rule["topic"] != CHOICE_TOPIC and rule.get("verification", "fact") == "fact":
        from agent_office.away_expert import assess
        # Only bounded reads of pinned Git objects run on this thread. The inbox
        # still owns final delivery and rechecks the operator's decision or revocation.
        return await asyncio.to_thread(assess, Path(repo), revision, rule, specialist_engine,
                                       verifier_engine, folders=folders)

    # Only the closest selected ancestor is needed for each evidence file.
    paths = ([option["path"] for option in rule["choice"]["options"]]
             if rule["topic"] == CHOICE_TOPIC else [rule["path"]])
    scope = list(dict.fromkeys(next((str(parent) for parent in PurePosixPath(path).parents
                                     if str(parent) in folders), None) for path in paths))
    payload = json.dumps([str(repo), revision, rule, specialist_engine, verifier_engine,
                          [folder for folder in scope if folder is not None], skill_guidance or {}],
                         ensure_ascii=False).encode("utf-8") + b"\n"
    if len(payload) > MAX_CONSULT_INPUT:
        raise DelegationDeferred("specialist_failed")
    child = await asyncio.create_subprocess_exec(
        *_command("supervise"), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=subprocess.DEVNULL, limit=16384, start_new_session=os.name != "nt",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    active_role = None
    try:
        child.stdin.write(payload)
        await child.stdin.drain()
        # Only the assess wrapper writes these fixed markers. Vendor output is
        # captured independently inside execute_fixture, including on failure.
        raw = await child.stdout.readline()
        while raw in STAGE_FRAMES:
            role, status = STAGE_FRAMES[raw]
            active_role = role if status == "RUNNING" else None
            if on_stage:
                on_stage(role, status)
            raw = await child.stdout.readline()
        trailing = await child.stdout.read(1)
    finally:
        # EOF cancels and drains the owned process tree before this attempt returns.
        child.stdin.close()
        try:
            exit_code = await child.wait()
        finally:
            if active_role and on_stage:
                on_stage(active_role, "INTERRUPTED")
    if exit_code or trailing or not raw or len(raw) > 16384:
        raise DelegationDeferred("verifier_failed" if active_role == "verifier" else "specialist_failed")
    result = json.loads(raw)
    if "reason" in result:
        raise DelegationDeferred(result["reason"])
    return result


def _supervise():
    payload = sys.stdin.buffer.readline(MAX_CONSULT_INPUT + 1)
    if not payload.endswith(b"\n") or len(payload) > MAX_CONSULT_INPUT:
        return 1
    # This supervisor outlives cancelled role processes. Keep their temporary
    # repositories underneath a directory it can remove once the tree is drained.
    # Other software on the host must keep using its ordinary temporary paths.
    with tempfile.TemporaryDirectory(prefix="office-away-consult-") as scratch:
        return 0 if run_until_owner_closes([*_command("assess"), scratch], payload, timeout=150) else 1


if __name__ == "__main__":
    if sys.argv[1:] == ["supervise"]:
        sys.exit(_supervise())
    if len(sys.argv) != 3 or sys.argv[1] != "assess":
        sys.exit(2)
    from agent_office.away_expert import assess
    from agent_office.technical_choices import CHOICE_TOPIC, assess_choice

    def emit_stage(role, status):
        sys.stdout.buffer.write(f"{role} {status}\n".encode("ascii"))
        sys.stdout.buffer.flush()

    repo, revision, rule, specialist, verifier, folders, guidance = json.load(sys.stdin.buffer)
    opts = dict(folders=folders, skill_guidance=guidance, scratch_root=Path(sys.argv[2]), on_stage=emit_stage)
    try:
        if rule["topic"] == CHOICE_TOPIC:
            response = assess_choice(Path(repo), revision, rule["choice"], specialist, verifier,
                                     goal=rule["goal"], **opts)
        else:
            response = assess(Path(repo), revision, rule, specialist, verifier, **opts)
    except DelegationDeferred as exc:
        response = {"reason": exc.reason}
    sys.stdout.buffer.write(json.dumps(response, ensure_ascii=False).encode("utf-8"))
