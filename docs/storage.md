# AO-001 local task journal

AO-001 persists tasks, runs and ordered phase events. It does not automatically resume a CLI session, replay interrupted commands, or serve an office UI.

Use a separate database for development candidates, outside the Git repository:

```text
agent-office --db ../ao-001-candidate-state/tasks.sqlite3 tasks list --limit 50
agent-office --db ../ao-001-candidate-state/tasks.sqlite3 tasks show <task_id>
agent-office --db ../ao-001-candidate-state/tasks.sqlite3 events <task_id> --after-seq 0 --limit 100
agent-office --db ../ao-001-candidate-state/tasks.sqlite3 recover --run-id <run_id>
```

The global --db argument precedes the subcommand. `run` accepts it too; a real run consumes the selected provider's usage. `doctor`, task queries and event queries never execute workers. Queries use read-only SQLite connections and never create, migrate or recover a database. Empty lists succeed; nonexistent IDs fail. Events return `next_after_seq` and `has_more` for polling.

Without --db, Windows uses `%LOCALAPPDATA%\AI-Agent-Office\stable\tasks.sqlite3`; Linux uses `${XDG_STATE_HOME:-~/.local/state}/ai-agent-office/stable/tasks.sqlite3`. Windows and WSL require separate local databases. Shared/network filesystems and sharing a database across hosts are unsupported. No server or port is used.

## States and interruptions

`PLAN_ONLY` means no tests or reviewer ran. `READY_FOR_HUMAN_REVIEW` means the configured tests and advisory reviewer passed; it does not authorize merging or deployment. Ambiguous review is `WAITING_USER`; there is no answer/resume command in this milestone.

The run lock is held before recording intent and through the final commit. Lock handles are not inherited by children. Persistent `<db>.locks/<run_id>.lock` files must not be deleted or replaced while using a database. Their presence alone does not mean a process is alive.

After coordinator interruption, first inspect tasks/events. Explicit `recover` takes the nonblocking OS lock, rechecks the state in a transaction and marks an unowned active run `RECOVERY_REQUIRED`. A live owner is rejected as RUN_BUSY. Repeating recovery of a terminal run changes nothing. Recovery never invokes Git, workers or tests and never kills children. A surviving child may still be running; recovery is not evidence of cancellation or rollback. Check the reported workspace and external process before deciding on a separate new task.

Worker returncode 124 and test timeout stop retries and require recovery review. Ctrl+C conservatively records interruption when possible and returns 130. An uncatchable kill leaves the last durable state for explicit recovery.

## Data, errors and retention

SQLite schema version is 1; newer or unrecognized schemas are rejected without migration. State/attempt/event sequence changes are committed together in short transactions. A failed start commit prevents external execution; a failed result commit stops the next stage and prevents a success report. A duplicate operation must match the full event and transition envelope to return the existing event.

Events contain allowlisted phase metadata, not raw prompt, source, stdout/stderr, environment or provider session IDs. Request snapshots mask known key/token patterns and are at most 8192 UTF-8 bytes. Snapshots are not replay inputs; arbitrary sensitive natural language cannot be perfectly detected. Worker prompts retain the original input in memory. Event JSON is at most 16384 UTF-8 bytes. These are persistence bounds, not a bound on existing subprocess capture memory.

There is no automatic deletion. While all runs are idle, the owner may archive a complete DB together with its lock directory and begin with a new path. Keep archives private; do not attach raw DBs/logs to public PRs. Do not replace a database under running coordinators. Full retention quotas and bounded subprocess streaming are prerequisites for future unattended operation.

Queries/recovery succeed with exit 0. Completed failed run reports use exit 1. Argument, Git, DB, schema or lock errors use exit 2 with fixed JSON error messages on stderr and empty stdout; they do not echo raw SQL parameters or subprocess stderr. Run JSON preserves the bootstrap status field and adds task_id/run_id/state/outcome. See [acceptance contract](superpowers/specs/2026-11-06-ao-001-acceptance.md) for exact envelopes and scenarios.
