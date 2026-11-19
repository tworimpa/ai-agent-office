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


def _command(mode):
    return [sys.executable, "-I", str(Path(__file__).resolve()), mode]


async def run_consultation(repo, revision, rule, specialist_engine, verifier_engine, folders, *, skill_guidance=None):
    # Only the closest selected ancestor is needed to check this evidence file.
    scope = next((str(parent) for parent in PurePosixPath(rule["path"]).parents if str(parent) in folders), None)
    payload = json.dumps([str(repo), revision, rule, specialist_engine, verifier_engine,
                          [scope] if scope else [], skill_guidance or {}], ensure_ascii=False).encode("utf-8") + b"\n"
    if len(payload) > MAX_CONSULT_INPUT:
        raise DelegationDeferred("specialist_failed")
    with tempfile.TemporaryFile() as output:
        child = await asyncio.create_subprocess_exec(
            *_command("supervise"), stdin=asyncio.subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            child.stdin.write(payload)
            await child.stdin.drain()
            status = await child.wait()
        finally:
            # EOF cancels and drains the owned process tree before this attempt returns.
            child.stdin.close()
            await child.wait()
        if status:
            raise DelegationDeferred("specialist_failed")
        output.seek(0)
        raw = output.read(16385)
    if not raw or len(raw) > 16384:
        raise DelegationDeferred("specialist_failed")
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

    repo, revision, rule, specialist, verifier, folders, guidance = json.load(sys.stdin.buffer)
    try:
        response = assess(Path(repo), revision, rule, specialist, verifier, folders=folders,
                          skill_guidance=guidance, scratch_root=Path(sys.argv[2]))
    except DelegationDeferred as exc:
        response = {"reason": exc.reason}
    sys.stdout.buffer.write(json.dumps(response, ensure_ascii=False).encode("utf-8"))
