"""Three-way comparison of portable bot definitions."""
from itertools import chain
import json

from . import bot_spec


_MISSING = object()


def path_conflicts(files, folders):
    """Return the file and explicit folder paths involved in a tree collision."""
    entries = {}
    conflicts = set()
    for path, is_folder in chain(((path, False) for path in files), ((path, True) for path in folders)):
        parts = bot_spec.check_path(path)
        display_path = path + "/" if is_folder else path
        for end in range(1, len(parts) + 1):
            prefix = "/".join(parts[:end])
            entry = (prefix, is_folder or end < len(parts))
            aliases = entries.setdefault(prefix.casefold(), {})
            aliases.setdefault(entry, set()).add(display_path)
    for aliases in entries.values():
        if len(aliases) > 1:
            conflicts.update(chain.from_iterable(aliases.values()))
    return sorted(conflicts)


def _identity(value):
    # JSON object order is immaterial, but booleans and numbers are distinct.
    if value is _MISSING:
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def compare(base, working, target):
    """Compare three validated versions of a bot and describe changes to working."""
    if not base["id"] == working["id"] == target["id"]:
        raise ValueError("비교할 봇 정의의 ID가 서로 다릅니다.")

    changes = []
    conflicts = []

    def merge_entries(original, local, incoming, *, prefix="", suffix=""):
        merged = {}
        for key in sorted(original.keys() | local.keys() | incoming.keys()):
            path = f"{prefix}{key}{suffix}"
            before, ours, theirs = (entries.get(key, _MISSING) for entries in (original, local, incoming))
            before_key, ours_key, theirs_key = map(_identity, (before, ours, theirs))
            if ours_key == theirs_key or theirs_key == before_key:
                if ours_key != before_key:
                    changes.append({"path": path, "action": "kept"})
                value = ours
            elif ours_key == before_key:
                if ours is _MISSING:
                    action = "added"
                elif theirs is _MISSING:
                    action = "deleted"
                else:
                    action = "modified"
                changes.append({"path": path, "action": action})
                value = theirs
            else:
                conflicts.append(path)
                continue
            if value is not _MISSING:
                merged[key] = value
        return merged

    documents = (base, working, target)
    labels = merge_entries(*({key: document[key] for key in ("name", "role")} for document in documents))
    files = merge_entries(*(document["files"] for document in documents))
    folders = list(merge_entries(*(dict.fromkeys(document.get("folders", []), True) for document in documents), suffix="/"))
    extensions = merge_entries(*(document.get("extensions", {}) for document in documents), prefix="extensions.")
    conflicts.extend(path_conflicts(files, folders))

    definition = None
    if not conflicts:
        merged = {"specVersion": bot_spec.VERSION, "id": base["id"], **labels,
                  "files": files, "folders": folders, "extensions": extensions}
        try:
            # Use the wire validator and detach nested extension values from all inputs.
            definition = bot_spec.loads(json.dumps(merged, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        except ValueError as error:
            conflicts.append(f"bot.json ({error})")
    return {"definition": definition, "conflicts": sorted(set(conflicts)),
            "changes": sorted(changes, key=lambda change: change["path"])}
