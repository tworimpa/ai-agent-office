from copy import deepcopy

import pytest

from agent_office import bot_spec
from agent_office.bot_package_merge import compare, path_conflicts


def manifest(*, files=None, **fields):
    return bot_spec.validate({
        "specVersion": "1.0", "id": "research", "name": "다온", "role": "조사",
        "files": {"SOUL.md": "관찰을 기록한다.", **(files or {})}, **fields,
    })


def test_disjoint_changes_are_applied_and_previewed_relative_to_working():
    base = manifest(files={"drop.md": "old", "edit.md": "before", "local-drop.md": "old"},
                    folders=["archive", "local-archive"])
    working = manifest(files={"drop.md": "old", "edit.md": "before", "local.md": "personal"},
                       folders=["archive", "local-empty"], name="나의 다온")
    target = manifest(files={"edit.md": "after", "local-drop.md": "old", "new.md": ""},
                      folders=["local-archive", "team-empty"], role="인터뷰")

    result = compare(base, working, target)

    assert result["conflicts"] == []
    assert result["definition"] == manifest(files={"edit.md": "after", "local.md": "personal", "new.md": ""},
                                            folders=["local-empty", "team-empty"], name="나의 다온", role="인터뷰")
    assert {entry["path"]: entry["action"] for entry in result["changes"]} == {
        "archive/": "deleted", "drop.md": "deleted", "edit.md": "modified", "local-archive/": "kept",
        "local-drop.md": "kept", "local-empty/": "kept", "local.md": "kept", "name": "kept",
        "new.md": "added", "role": "modified", "team-empty/": "added",
    }


@pytest.mark.parametrize("base,working,target", [
    ("original", "ours", "theirs"),
    ("original", None, "edited"),
    ("original", "edited", None),
    (None, "ours", "theirs"),
    ("line 1\nline 2\n", "edit 1\nline 2\n", "line 1\nedit 2\n"),
])
def test_different_concurrent_file_changes_conflict_as_whole_text(base, working, target):
    documents = [manifest(files={} if value is None else {"notes/a.md": value})
                 for value in (base, working, target)]

    result = compare(*documents)

    assert result["definition"] is None
    assert result["conflicts"] == ["notes/a.md"]
    assert result["changes"] == []


def test_identical_concurrent_changes_keep_the_shared_result():
    base = manifest(files={"removed.md": "old", "notes.md": "old"}, folders=["old"])
    updated = manifest(files={"notes.md": "new", "empty.md": ""}, folders=["new"])

    result = compare(base, updated, deepcopy(updated))

    assert result["conflicts"] == []
    assert result["definition"] == updated
    assert {entry["path"]: entry["action"] for entry in result["changes"]} == {
        "empty.md": "kept", "new/": "kept", "notes.md": "kept", "old/": "kept", "removed.md": "kept",
    }


def test_extensions_merge_by_namespace_with_null_distinct_from_missing():
    base = manifest(extensions={"org.example.remove": None})
    working = manifest(extensions={"org.example.remove": None, "org.example.local": None})
    target = manifest(extensions={"org.example.new": None})

    result = compare(base, working, target)

    assert result["conflicts"] == []
    assert result["definition"]["extensions"] == {"org.example.local": None, "org.example.new": None}
    assert {entry["path"]: entry["action"] for entry in result["changes"]} == {
        "extensions.org.example.local": "kept", "extensions.org.example.new": "added",
        "extensions.org.example.remove": "deleted",
    }


def test_concurrent_extension_and_metadata_changes_report_each_conflicting_field():
    appearance = bot_spec.APPEARANCE
    base = manifest(extensions={"org.example.flag": None, appearance: {"preset": 1}})
    working = manifest(name="로컬 다온", extensions={"org.example.value": {"enabled": True}, appearance: {"preset": 2}})
    target = manifest(name="공유 다온", extensions={"org.example.flag": False, "org.example.value": {"enabled": 1},
                                               appearance: {"preset": 3}})

    result = compare(base, working, target)

    assert result["definition"] is None
    assert set(result["conflicts"]) == {
        "name", f"extensions.{appearance}", "extensions.org.example.flag", "extensions.org.example.value",
    }


def test_equal_extension_objects_ignore_key_order_and_result_does_not_alias_inputs():
    namespace = "org.example.value"
    base = manifest()
    working = manifest(extensions={namespace: {"a": 1, "b": [2, 3]}})
    target = manifest(extensions={namespace: {"b": [2, 3], "a": 1}})
    original = deepcopy((base, working, target))

    result = compare(base, working, target)

    assert result["conflicts"] == []
    assert result["definition"]["extensions"] == {namespace: {"a": 1, "b": [2, 3]}}
    result["definition"]["extensions"][namespace]["b"].append(4)
    assert (base, working, target) == original


@pytest.mark.parametrize("working,target,expected", [
    ({"files": {"Strasse/ours.md": "ours"}}, {"files": {"Straße/theirs.md": "theirs"}},
     ["Strasse/ours.md", "Straße/theirs.md"]),
    ({"files": {"data": "file"}}, {"folders": ["data"]}, ["data", "data/"]),
    ({"files": {"data": "file"}}, {"files": {"data/a.md": "child"}}, ["data", "data/a.md"]),
    ({"folders": ["Empty"]}, {"folders": ["empty"]}, ["Empty/", "empty/"]),
])
def test_cross_branch_path_collisions_report_both_entries(working, target, expected):
    result = compare(manifest(), manifest(**working), manifest(**target))

    assert result["definition"] is None
    assert result["conflicts"] == expected


def test_path_conflicts_can_check_reattached_local_memory():
    assert path_conflicts({"pack/MEMORY.md", "pack/memory.md/child"}, []) == [
        "pack/MEMORY.md", "pack/memory.md/child",
    ]
    assert path_conflicts({"data/a.md", "data/b.md", "MEMORY.md"}, ["data", "data/empty"]) == []


@pytest.mark.parametrize("working,target,limit", [
    ({"files": {f"local-{i}": "" for i in range(20)}},
     {"files": {f"shared-{i}": "" for i in range(20)}}, "32개 파일"),
    ({"files": {"local.md": "a" * 8192, "old.md": "z" * 8192}},
     {"files": {"shared.md": "b" * 8192, "new.md": "c" * 8192}}, "24 KiB"),
    ({"folders": [f"local-{i}" for i in range(20)]},
     {"folders": [f"shared-{i}" for i in range(20)]}, "32개까지"),
    ({"extensions": {f"org.local.v{i}": None for i in range(9)}},
     {"extensions": {f"org.shared.v{i}": None for i in range(9)}}, "16개"),
    ({"extensions": {"org.local": "a" * (90 * 1024)}},
     {"extensions": {"org.shared": "b" * (90 * 1024)}}, "160 KiB"),
])
def test_individually_valid_definitions_cannot_merge_past_combined_limits(working, target, limit):
    result = compare(manifest(), manifest(**working), manifest(**target))

    assert result["definition"] is None
    assert len(result["conflicts"]) == 1
    assert limit in result["conflicts"][0]
