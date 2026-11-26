"""Durable role assignments, legacy reads, concurrent creation and run pinning."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from agent_office import local_bots as bots
from agent_office.models import OfficeError
from agent_office.store import TaskStore, encode


def test_older_workspace_reads_builtin_roles_without_schema_changes(tmp_path):
    path, workspace = tmp_path / "tasks.db", uuid4().hex
    with TaskStore(path):
        pass
    with TaskStore(path, readonly=True) as store:
        db = store.connection
        listed = bots.list_bots(db, workspace)
        assert [bot["id"] for bot in listed] == [f"office-{role}" for role in bots.ROLES]
        assert bots.resolve(db, workspace) == {bot["role"]: bot for bot in listed}
        assert all(bots.get(db, workspace, bot["id"]) == bot and bot["builtin"] for bot in listed)
        assert bots.for_run(db, uuid4().hex) == {}
        with pytest.raises(OfficeError, match="NOT_FOUND"):
            bots.get(db, workspace, "unknown")
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='studio_local_bots'").fetchone()


def test_role_bindings_are_scoped_and_snapshots_survive_rebind_and_reopen(tmp_path):
    path = tmp_path / "tasks.db"
    workspace, other, run = (uuid4().hex for _ in range(3))
    with TaskStore(path) as store, store._write():
        db = store.connection
        bots.ensure_schema(db)
        for role in bots.ROLES:
            definition = bots.create(db, workspace, f"my-{role}", f" {role} ", role, f"지침\r\n{role}")
            assert definition["name"] == role
            assert bots.assign(db, workspace, role, definition["id"]) == definition
        with pytest.raises(OfficeError, match="NOT_FOUND"):
            bots.assign(db, other, "developer", "my-developer")
        with pytest.raises(ValueError):
            bots.assign(db, workspace, "reviewer", "my-developer")
        snapshot = bots.resolve(db, workspace)
        receipt = bots.public_receipt(snapshot)
        assert bots.validate_receipt(receipt)
        assert all("instructions" not in bot for bot in receipt.values())
        assert not bots.validate_receipt({**receipt, "reviewer": receipt["developer"]})
        assert not bots.validate_receipt({**receipt, "reviewer": snapshot["reviewer"]})
        bots.pin(db, run, snapshot)
        bots.pin(db, run, snapshot)
        bots.assign(db, workspace, "developer", "office-developer")
        with pytest.raises(ValueError):
            bots.pin(db, run, bots.resolve(db, workspace))
    with TaskStore(path, readonly=True) as store:
        db = store.connection
        assert bots.for_run(db, run) == snapshot
        assert bots.resolve(db, workspace)["developer"]["builtin"]
        assert all(bot["builtin"] for bot in bots.resolve(db, other).values())
        assert len(bots.list_bots(db, workspace)) == 6


def test_concurrent_creation_retries_and_conflicting_definition(tmp_path):
    path, workspace = tmp_path / "tasks.db", uuid4().hex
    with TaskStore(path) as store, store._write():
        bots.ensure_schema(store.connection)

    def create(_):
        with TaskStore(path, existing=True) as store, store._write():
            return bots.create(store.connection, workspace, "build-bot", "Build", "developer", "Check tests.")

    with ThreadPoolExecutor(max_workers=4) as pool:
        definitions = list(pool.map(create, range(8)))
    assert all(definition == definitions[0] for definition in definitions)
    with TaskStore(path) as store, store._write():
        db = store.connection
        with pytest.raises(ValueError):
            bots.create(db, workspace, "build-bot", "Build", "developer", "A different definition")
        assert bots.get(db, workspace, "build-bot") == definitions[0]
        other = bots.create(db, uuid4().hex, "build-bot", "Build", "developer", "Different workspace")
        assert other["digest"] != definitions[0]["digest"]


@pytest.mark.parametrize("bot_id,name,role,instructions", [
    ("office-coordinator", "x", "coordinator", "a"), ("Bad.ID", "x", "reviewer", "a"),
    ("../dev", "x", "developer", "a"), ("a" * 65, "x", "reviewer", "a"),
    ("dev", "a\nb", "developer", "a"), ("dev", " ", "developer", "a"),
    ("dev", "a" * 41, "developer", "a"), ("dev", "x", "unknown", "a"),
    ("dev", "x", "developer", "가" * 2731), ("dev", "x", "developer", " \t"),
    ("dev", "x", "developer", "a\x00"), ("dev", "x", "developer", {}),
])
def test_rejects_invalid_definitions(tmp_path, bot_id, name, role, instructions):
    workspace = uuid4().hex
    with TaskStore(tmp_path / "tasks.db") as store, store._write():
        db = store.connection
        bots.ensure_schema(db)
        with pytest.raises(ValueError):
            bots.create(db, workspace, bot_id, name, role, instructions)
        assert all(bot["builtin"] for bot in bots.list_bots(db, workspace))


def test_corrupt_definitions_and_snapshots_fail_before_execution(tmp_path):
    workspace, run = uuid4().hex, uuid4().hex
    with TaskStore(tmp_path / "tasks.db") as store, store._write():
        db = store.connection
        bots.ensure_schema(db)
        bot = bots.create(db, workspace, "review", "리뷰어", "reviewer", "Inspect tests.")
        bots.assign(db, workspace, "reviewer", "review")
        selected = bots.resolve(db, workspace)
        bots.pin(db, run, selected)
        tampered = {**bot, "instructions": "Changed"}
        db.execute("UPDATE studio_local_bots SET definition=?", (encode(tampered),))
        with pytest.raises(OfficeError, match="STORE_IO"):
            bots.resolve(db, workspace)
        assert bots.for_run(db, run) == selected
        db.execute("UPDATE studio_run_role_bots SET snapshot=?",
                   (encode({**selected, "reviewer": tampered}),))
        with pytest.raises(OfficeError, match="STORE_IO"):
            bots.for_run(db, run)
