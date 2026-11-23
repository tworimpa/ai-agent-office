"""Per-host model catalog, read-only quota probes and per-call CLI selection."""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
import tempfile
import threading
import time

from .acp import _unique_fields, close_stdio_process
from .engines import codex_backend_args, resolve_executable
from .events import redact_request
from .github_connection import worker_env
from .provider_telemetry import quota_response, safe_slug


CAPABILITIES = {
    "codex": dict(model="supported", effort="supported", usage="supported", limits="supported"),
    "claude": dict(model="supported", effort="supported", usage="supported", limits="supported"),
    "antigravity": dict(model="supported", effort="supported", usage="supported", limits="unknown"),
    "gemini": dict(model="supported", effort="unsupported", usage="supported", limits="unknown"),
    "cline": dict(model="unsupported", effort="unsupported", usage="supported", limits="unknown"),
}


def host_capabilities(engines, catalog):
    result = deepcopy(CAPABILITIES)
    by_name = {row["engine"]: row for row in engines}
    for engine, capabilities in result.items():
        entry = by_name.get(engine, {})
        for field in ("model", "effort", "usage", "limits"):
            if capabilities[field] != "supported":
                continue
            if not entry.get("installed") or entry.get("probe_status") != "ok":
                capabilities[field] = "unknown"
            elif engine == "codex" and field in ("model","effort") and catalog["status"] != "available":
                capabilities[field] = "unknown"
            elif engine != "codex" and field in ("model","effort"):
                flags = entry.get("profile_flags")
                capabilities[field] = ("unknown" if flags is None else "supported" if "--"+field in flags else "unsupported")
    return result


def validate_selection(engine, model=None, effort=None):
    if engine not in CAPABILITIES:
        raise ValueError("Unknown provider.")
    for name, value in (("model", model), ("effort", effort)):
        if value is not None and (CAPABILITIES[engine][name] != "supported" or not safe_slug(value)):
            raise ValueError(f"Invalid or unsupported {name} for {engine}.")
    if effort is not None and engine in ("claude", "antigravity") and effort not in ("low", "medium", "high", "xhigh", "max"):
        raise ValueError(f"Unsupported reasoning effort for {engine}.")


def _models(page):
    data = page.get("data")
    if not isinstance(data, list) or len(data) > 100:
        raise ValueError("Invalid model catalog page.")
    for row in data:
        if not isinstance(row, dict) or row.get("hidden") is True:
            continue
        model = row.get("model")
        if not safe_slug(model):
            continue
        options = row.get("supportedReasoningEfforts", [])
        if not isinstance(options, list) or len(options) > 32:
            raise ValueError("Invalid catalog effort options.")
        efforts = list(dict.fromkeys(opt["reasoningEffort"] for opt in options
                      if isinstance(opt, dict) and safe_slug(opt.get("reasoningEffort"))))
        label = row.get("displayName")
        label = label if isinstance(label, str) and 0 < len(label) <= 96 and label.isprintable() and redact_request(label) == label else model
        default = row.get("defaultReasoningEffort")
        yield {"id": model, "label": label, "efforts": efforts, "default_effort": default if default in efforts else None,
               "is_default": row.get("isDefault") is True}


async def _metadata(operation):
    executable = resolve_executable("codex")
    if executable is None:
        raise ValueError("Codex is unavailable.")
    with tempfile.TemporaryDirectory(prefix="agent-office-metadata-") as cwd:
        async with asyncio.timeout(10):
            process = await asyncio.create_subprocess_exec(
                executable, *codex_backend_args(), "app-server", "--listen", "stdio://", cwd=cwd,
                start_new_session=os.name != "nt", env=worker_env(os.environ), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=1024 * 1024)
            try:
                sequence = 0
                remaining = 4 * 1024 * 1024

                async def send(message):
                    process.stdin.write((json.dumps(message) + "\n").encode())
                    await process.stdin.drain()

                async def rpc(method, params):
                    nonlocal sequence, remaining
                    sequence += 1
                    await send({"id": sequence, "method": method, "params": params})
                    for _ in range(64):
                        line = await process.stdout.readline()
                        remaining -= len(line)
                        if not line or remaining < 0:
                            break
                        msg = json.loads(line, object_pairs_hook=_unique_fields)
                        if not isinstance(msg, dict):
                            break
                        if "id" not in msg and isinstance(msg.get("method"), str):
                            continue
                        if ("method" not in msg and type(msg.get("id")) is int and msg["id"] == sequence
                                and "error" not in msg and isinstance(msg.get("result"), dict)):
                            return msg["result"]
                        break
                    raise ValueError("Invalid metadata response.")

                await rpc("initialize", {"clientInfo": {"name": "agent_office", "version": "0.1.0"}, "capabilities": {}})
                await send({"method": "initialized", "params": {}})
                if operation == "quota":
                    result = await rpc("account/rateLimits/read", {"excludeResetCreditDetails": True})
                    return quota_response(result)[1]
                if operation != "catalog":
                    raise ValueError("Unknown metadata operation.")
                models, cursors = {}, set()
                params = {"includeHidden": False, "limit": 100}
                for _ in range(8):
                    page = await rpc("model/list", params)
                    for model in _models(page):
                        if model["id"] in models and model != models[model["id"]]:
                            raise ValueError("Ambiguous catalog entry.")
                        models[model["id"]] = model
                    cursor = page.get("nextCursor")
                    if cursor is None:
                        return list(models.values())
                    if not isinstance(cursor, str) or not 0 < len(cursor) <= 1024 or cursor in cursors:
                        break
                    cursors.add(cursor)
                    params["cursor"] = cursor
                raise ValueError("Invalid catalog pagination.")
            finally:
                await close_stdio_process(process)


def probe_quota(engine):
    if engine != "codex":
        return []
    try:
        return asyncio.run(_metadata("quota"))
    except (ValueError, OSError, RecursionError):
        return []


class ModelCatalog:
    """An explicit refresh replaces one complete host-local snapshot atomically."""
    def __init__(self):
        self._refresh_lock = threading.Lock()
        self._snapshot = {"engine": "codex", "status": "unprobed", "models": [], "refreshed_at": None}
        self._expires = 0.

    def read(self):
        return deepcopy(self._snapshot)

    def refresh(self, *, if_stale=False):
        with self._refresh_lock:
            if if_stale and time.monotonic() < self._expires:
                return self.read()
            try:
                models = asyncio.run(_metadata("catalog"))
                status = "available"
            except (ValueError, OSError, RecursionError):
                status, models = "unavailable", []
            self._snapshot = {"engine": "codex", "models": models, "status": status,
                              "refreshed_at": datetime.now(timezone.utc).isoformat()}
            self._expires = time.monotonic() + (300 if status == "available" else 30)
            return self.read()

    def validate(self, engine, model=None, effort=None):
        validate_selection(engine, model, effort)
        if engine != "codex" or (model is None and effort is None):
            return
        catalog = self.read()
        rows = [row for row in catalog["models"] if (row["id"] == model if model is not None else row["is_default"])]
        if catalog["status"] != "available" or len(rows) != 1 or effort is not None and effort not in rows[0]["efforts"]:
            raise ValueError("Requested Codex model or reasoning effort is unavailable in the execution host catalog.")
