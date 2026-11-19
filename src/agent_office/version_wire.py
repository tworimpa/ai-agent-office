"""Read-only routes and the SSH reply contract for published stable versions."""
from datetime import datetime
import re


PAGE_SIZE = 24
MAX_SEQUENCE = 2**53 - 1


def version_route(method, path):
    if method != "GET":
        return None
    page = re.fullmatch(r"/versions/page/(0|[1-9][0-9]{0,15})", path)
    if page:
        offset = int(page[1])
        if offset <= MAX_SEQUENCE and offset % PAGE_SIZE == 0:
            return ("page", offset)
    pair = re.fullmatch(r"/versions/compare/([1-9][0-9]{0,15})/([1-9][0-9]{0,15})", path)
    if pair:
        start, end = map(int, pair.groups())
        if max(start, end) <= MAX_SEQUENCE:
            return ("compare", start, end)
    return None


def _check(condition):
    if not condition:
        raise ValueError("Invalid stable version response")


def _keys(value, names):
    _check(isinstance(value, dict) and value.keys() == set(names.split()))


def _sha(value):
    _check(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{40}(?:[a-f0-9]{24})?", value))


def _stamp(value):
    _check(isinstance(value, str) and len(value) <= 48 and datetime.fromisoformat(value).tzinfo is not None)


def _entry(row):
    _keys(row, "seq previous sha kind time target_seq")
    seq = row["seq"]
    _check(type(seq) is int and 1 <= seq <= MAX_SEQUENCE)
    _sha(row["sha"])
    if seq == 1:
        _check(row["kind"] == "initial" and row["previous"] is None and row["target_seq"] is None)
    else:
        _sha(row["previous"])
        _check(len(row["previous"]) == len(row["sha"]) and row["previous"] != row["sha"])
        if row["kind"] == "promotion":
            _check(row["target_seq"] is None)
        else:
            _check(row["kind"] == "rollback" and type(row["target_seq"]) is int and 1 <= row["target_seq"] < seq - 1)
    if seq != 1 or row["time"] is not None:
        _stamp(row["time"])


def _page(data, offset):
    _keys(data, "available current updated_at items next_offset")
    rows = data["items"]
    _check(type(data["available"]) is bool and isinstance(rows, list))
    if not data["available"]:
        _check(rows == [] and all(data[k] is None for k in ("current", "updated_at", "next_offset")))
        return
    current = data["current"]
    _entry(current)
    _stamp(data["updated_at"])
    remaining = max(0, current["seq"] - offset)
    _check(len(rows) == min(PAGE_SIZE, remaining))
    cursor = data["next_offset"]
    if remaining > PAGE_SIZE:
        _check(type(cursor) is int and cursor == offset + PAGE_SIZE)
    else:
        _check(cursor is None)
    for i, row in enumerate(rows):
        _entry(row)
        _check(row["seq"] == remaining - i)
        if i:
            _check(rows[i-1]["previous"] == row["sha"])
    if offset == 0:
        _check(rows[0] == current)
    for row in [current, *rows]:
        target = row["target_seq"]
        if target is not None and 0 <= remaining - target < len(rows):
            _check(rows[remaining - target]["sha"] == row["sha"])


def _comparison(data, start, end):
    _keys(data, "from_seq to_seq from_sha to_sha files total_files diff truncated")
    for side, seq in (("from", start), ("to", end)):
        value = data[side + "_seq"]
        _check(type(value) is int and value == seq)
        _sha(data[side + "_sha"])
    _check(len(data["from_sha"]) == len(data["to_sha"]))
    total, rows = data["total_files"], data["files"]
    _check(type(total) is int and 0 <= total <= MAX_SEQUENCE and isinstance(rows, list))
    _check(len(rows) == min(total, 100) and type(data["truncated"]) is bool)
    _check(isinstance(data["diff"], str) and len(data["diff"].encode("utf-8")) <= 65536)
    _check(total <= len(rows) or data["truncated"])
    if start == end:
        _check(data["from_sha"] == data["to_sha"])
    if data["from_sha"] == data["to_sha"]:
        _check(total == 0)
    if total == 0:
        _check(not data["diff"] and not data["truncated"])
    else:
        _check(bool(data["diff"]))
    seen = set()
    for file in rows:
        _keys(file, "path status before_mode after_mode")
        path = file["path"]
        _check(isinstance(path, str) and 0 < len(path.encode("utf-8")) <= 1024 and path not in seen)
        _check(not any(p in ("", ".", "..") for p in path.split("/")) and "\\" not in path)
        seen.add(path)
        before, after = file["before_mode"], file["after_mode"]
        _check(before in ("100644", "100755", None) and after in ("100644", "100755", None))
        _check(before is not None or after is not None)
        expected = "added" if before is None else "deleted" if after is None else "modified"
        _check(file["status"] == expected)


def check_version_reply(status, data, target):
    _check(status == 200)
    if target[0] == "page":
        _page(data, target[1])
    else:
        _comparison(data, *target[1:])
