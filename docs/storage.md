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

Database hard links are unsupported and rejected: aliases of one file must not acquire different run locks. Recovery opens existing databases only and validates the managed schema without initializing a foreign or empty file.

`PLAN_ONLY` means no tests or reviewer ran. `READY_FOR_HUMAN_REVIEW` means the configured tests and advisory reviewer passed; it does not authorize merging or deployment. Ambiguous review is `WAITING_USER`; there is no answer/resume command in this milestone.

The run lock is held before recording intent and through the final commit. Lock handles are not inherited by children. Persistent `<db>.locks/<run_id>.lock` files must not be deleted or replaced while using a database. Their presence alone does not mean a process is alive.

After coordinator interruption, first inspect tasks/events. Explicit `recover` takes the nonblocking OS lock, rechecks the state in a transaction and marks an unowned active run `RECOVERY_REQUIRED`. A live owner is rejected as RUN_BUSY. Repeating recovery of a terminal run changes nothing. Recovery never invokes Git, workers or tests and never kills children. A surviving child may still be running; recovery is not evidence of cancellation or rollback. Check the reported workspace and external process before deciding on a separate new task.

Worker returncode 124 and test timeout stop retries and require recovery review. Ctrl+C conservatively records interruption when possible and returns 130. An uncatchable kill leaves the last durable state for explicit recovery.

## Data, errors and retention

A new standalone desktop or Studio journal uses SQLite WAL with the default durable synchronization setting. Host the journal on a local filesystem whose owner can write the adjacent `-wal` and `-shm` files. To create a rollback-mode standalone journal for a split-account deployment, set `AGENT_OFFICE_SQLITE_JOURNAL=delete` before its first start. Managed runtime startup, workers, the deployment controller, and existing or read-only journals preserve the stored mode. Use SQLite's backup API or `.backup` for a live snapshot. For a filesystem archive, first stop every connection so SQLite can finish its WAL checkpoint.

SQLite integer budgets/cursors must fit the signed 64-bit range. An omitted run ID selects the task's current run; an explicitly empty ID is invalid. Truncated reviewer responses are treated as undecided because a trailing contradictory verdict may have been omitted.

SQLite schema version is 1; newer or unrecognized schemas are rejected without migration. State/attempt/event sequence changes are committed together in short transactions. A failed start commit prevents external execution; a failed result commit stops the next stage and prevents a success report. A duplicate operation must match the full event and transition envelope to return the existing event.

Events contain allowlisted phase metadata, not raw prompt, source, stdout/stderr, environment or provider session IDs. Request snapshots mask known key/token patterns and are at most 8192 UTF-8 bytes. Snapshots are not replay inputs; arbitrary sensitive natural language cannot be perfectly detected. Worker prompts retain the original input in memory. Event JSON is at most 16384 UTF-8 bytes. These are persistence bounds, not a bound on existing subprocess capture memory.

There is no automatic deletion. While all runs are idle, the owner may archive a complete DB together with its lock directory and begin with a new path. Keep archives private; do not attach raw DBs/logs to public PRs. Do not replace a database under running coordinators. Full retention quotas and bounded subprocess streaming are prerequisites for future unattended operation.

Queries/recovery succeed with exit 0. Completed failed run reports use exit 1. Argument, Git, DB, schema or lock errors use exit 2 with fixed JSON error messages on stderr and empty stdout; they do not echo raw SQL parameters or subprocess stderr. Run JSON preserves the bootstrap status field and adds task_id/run_id/state/outcome. See [acceptance contract](superpowers/specs/2026-11-06-ao-001-acceptance.md) for exact envelopes and scenarios.

## 작업 트리 정리

작업실 설정에서 **작업 트리 저장 공간 → 사용량과 보존 사유 조회**를 선택한다. 파일 크기는 작업 트리 아래 일반 파일의 논리 크기 합계다. 현재 호스트의 등록 폴더에 속한 이 작업실 실행과 같은 저장소의 CLI 실행을 표시한다. 실행 기록이나 Git 공용 객체 저장소는 파일 크기에서 제외한다.

실행 종료 후 7일이 지나고, 체크아웃이 깨끗하며 임시 브랜치의 끝 커밋이 등록 저장소의 현재 HEAD에 포함된 경우에 정리할 수 있다. 실행기·하위 작업·잠금, 대기 중인 응답, 복구·체크포인트, 검토 중인 후보, 무시된 파일과 경로 연결도 확인한다. 사용자가 한 건을 선택하면 실행 호스트는 저널·실행 ID와 조회 당시 커밋을 검사하고 실행 잠금과 작업자 잠금을 확보한다. 실행 호스트는 원본 폴더의 파일 ID와 같은 파일시스템에 마련한 전용 보관 경로를 저널에 기록한 뒤 체크아웃을 옮겨 재검사한다. 정리할 때는 Git이 확인한 추적 파일만 제거하고 폴더에는 빈 디렉터리 삭제만 사용한다. 이동 전부터 열려 있던 에디터가 뒤늦게 파일을 쓰면 화면에 보존 위치가 표시된다. 추적 파일 정리가 이미 시작된 경우 커밋된 내용은 임시 브랜치에서 확인할 수 있다. Git에는 부모 경로가 일반 파일로 예약되어 실제 체크아웃이 존재할 수 없는 경로만 전달하여 등록을 해제한다. 브랜치 삭제 단계에서는 Git 트랜잭션으로 등록된 체크아웃 HEAD를 잠그고 조회한 SHA와 일치하는 참조만 지운다. 동시에 새 체크아웃이 등록되면 원래 커밋을 보존한다. Git이 필요한 참조 잠금을 지원하지 않거나 다른 작업과 겹치면 브랜치를 남긴다. 중단 후에는 다시 조회하여 같은 커밋으로 재시도할 수 있다.

## 코딩 작업자 자식 프로세스

공급자 CLI와 저장소 테스트는 호출별 전용 환경에서 표준 Git 전송 프로토콜을 차단하고, Git 자격 증명 도우미와 SSH 인증 소켓을 비활성화한다. 로컬 Git 상태 조회는 가능하다. 작업자 PATH에서 일반적인 gh 호출을 차단하고 독립된 빈 GH_CONFIG_DIR를 사용한다. Studio의 GitHub 연결과 운영자 Git 환경은 별도로 동작한다. 이 설정은 일반적인 실수로 인한 외부 쓰기를 줄인다. 임의 코드가 환경을 바꾸거나 다른 실행 파일·네트워크 API를 사용하는 경우의 격리에는 OS 수준 통제가 필요하다.
