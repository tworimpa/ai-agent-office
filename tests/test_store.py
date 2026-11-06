import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest

TASK = "1" * 32
RUN = "2" * 32


def seeded(path):
    from agent_office.models import TaskSpec, OwnerInfo
    from agent_office.store import TaskStore
    from agent_office.workspace import Worktree
    store = TaskStore(path)
    store.create_run(TaskSpec(TASK, "project", "한글 request", "claude", "codex", 2),
                     RUN, Worktree(path.parent / "작업 space", "ao/test"), OwnerInfo("host", "owner"))
    return store


def test_reopen_preserves_tasks_and_ordered_events(tmp_path):
    from agent_office.store import TaskStore
    path = tmp_path / "한글 space" / "tasks.sqlite3"
    with seeded(path) as store:
        store.record_transition(RUN, operation_key="start", event_type="STAGE_STARTED",
                                state="RUNNING", attempts=1, payload={"phase": "developer"})
    with TaskStore(path, readonly=True) as reader:
        assert reader.get_task(TASK).state == "RUNNING"
        assert reader.get_task(TASK).attempts == 1
        assert [e.seq for e in reader.list_events(TASK)] == [1, 2]
        assert reader.list_events(TASK, after_seq=1)[0].event_type == "STAGE_STARTED"


def test_transition_and_event_rollback_together(tmp_path):
    with seeded(tmp_path / "db") as store:
        store.connection.execute("CREATE TRIGGER fail_event BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'fail'); END")
        with pytest.raises(sqlite3.IntegrityError):
            store.record_transition(RUN, operation_key="start", event_type="STAGE_STARTED", state="RUNNING", attempts=1)
        assert store.get_task(TASK).state == "QUEUED"
        assert store.get_run(RUN).next_seq == 2
        assert len(store.list_events(TASK)) == 1


def test_same_operation_is_idempotent_and_changed_target_rejected(tmp_path):
    with seeded(tmp_path / "db") as store:
        args = dict(operation_key="start", event_type="STAGE_STARTED", state="RUNNING", attempts=1)
        first = store.record_transition(RUN, **args)
        assert store.record_transition(RUN, **args) == first
        assert len(store.list_events(TASK)) == 2
        with pytest.raises(ValueError):
            store.record_transition(RUN, **{**args, "attempts": 2})
        with pytest.raises(ValueError):
            store.record_transition(RUN, **{**args, "state": "FAILED", "outcome": "TEST_FAILED"})


def test_two_connections_assign_unique_sequence(tmp_path):
    from agent_office.store import TaskStore
    path = tmp_path / "db"
    seeded(path).close()
    def append(i):
        with TaskStore(path) as store:
            return store.record_transition(RUN, operation_key=f"meta:{i}", event_type="WORKSPACE_READY").seq
    with ThreadPoolExecutor(max_workers=4) as pool:
        seqs = list(pool.map(append, range(20)))
    assert sorted(seqs) == list(range(2, 22))


def test_second_active_run_for_task_is_rejected(tmp_path):
    from agent_office.models import TaskSpec, OwnerInfo
    from agent_office.workspace import Worktree
    with seeded(tmp_path / "db") as store:
        with pytest.raises(ValueError):
            store.create_run(TaskSpec(TASK, "project", "x", "claude", "codex", 2), "3" * 32,
                             Worktree(tmp_path, "ao/other"), OwnerInfo("host", "other"))
        assert len(store.list_events(TASK)) == 1


def test_readonly_missing_db_does_not_create(tmp_path):
    from agent_office.store import TaskStore
    path = tmp_path / "missing" / "db"
    with pytest.raises(Exception) as error:
        TaskStore(path, readonly=True)
    assert error.value.code == "DB_NOT_FOUND"
    assert not path.parent.exists()


def test_newer_schema_is_not_modified(tmp_path):
    from agent_office.store import TaskStore
    path = tmp_path / "db"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=2")
    before = path.read_bytes()
    with pytest.raises(Exception) as error:
        TaskStore(path)
    assert error.value.code == "SCHEMA_UNSUPPORTED"
    assert path.read_bytes() == before


@pytest.mark.parametrize("change", [dict(state="READY_FOR_HUMAN_REVIEW"),dict(attempts=3),dict(attempts=-1),dict(state="WHAT")])
def test_invalid_transition_or_budget_rolls_back(tmp_path, change):
    with seeded(tmp_path / "db") as store:
        with pytest.raises(ValueError):
            store.record_transition(RUN, operation_key="bad", event_type="STAGE_STARTED", **change)
        assert store.get_task(TASK).state == "QUEUED"
        assert len(store.list_events(TASK)) == 1
