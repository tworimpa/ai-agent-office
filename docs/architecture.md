# AO-000 Architecture & Scope

## Intent

Build a personal Windows/Linux AI coding team orchestrator that can eventually improve its own implementation. Agents are existing CLI coding assistants, not reimplementations of their internal tool loops.

## Architectural boundaries

1. **Task Intake:** structured task specification from the user (AO-000: CLI arguments).
2. **LangGraph:** deterministic routing of development, test and review, with bounded retries.
3. **CLI Adapter Layer:** command construction and output normalization for Claude Code and Codex (AO-000). Planned: Gemini, Cline, Antigravity.
4. **Workspace:** new Git worktree per task; no direct writes to running install.
5. **Evidence:** local test output + reviewer response + isolated workspace. Reviewers are advisory, not deployment authority.
6. **Approval boundary:** AO-000 always stops before merge, push or deploy.

## Proposed future workflow

```text
Intake -> Planner -> Worker assignment -> CLI adapter
            |                                    |
            +---- Question Router <--------------+
                     |
         Policy -> Delegate / User Inbox
                     |
                  Resume
                     |
             Test -> Independent Review
                     |
             Human-approved promotion
```

**AO-001 candidate:** local SQLite task/run/phase-event journal, read-only task/event queries, per-run OS ownership and explicit interrupted-run marking. See [storage guide](storage.md). This is phase-event polling, not provider tool-call streaming.

**Not yet implemented:** planner, automatic queue consumption, checkpoint resume, question interception, session resume, automatic delegation, UI, GitHub PR automation, deploy management.

## Worktree vs sandbox

Git worktrees isolate the *git working directory*; they do not isolate processes, network access, secrets, user home or other filesystem paths. A production-grade autonomous mode must add OS-level process sandboxing, restricted credentials and explicit policy gates at tool execution boundaries.

## Task completion states

- `PLAN_ONLY`: developer produced a response in no-edit mode; no tests/review ran.
- `FAILED`: developer CLI returned nonzero.
- `TEST_FAILED`: tests failed after bounded attempts.
- `REVIEW_FAILED`: reviewer CLI returned nonzero.
- `NEEDS_HUMAN_REVIEW`: unknown reviewer verdict.
- `CHANGES_REQUESTED`: reviewer requested changes but retry budget exhausted.
- `READY_FOR_HUMAN_REVIEW`: tests passed and reviewer returned strict approval marker. Not merged.

## Decisions made for bootstrap

- Python 3.11+ / LangGraph for orchestration.
- stdlib subprocess for local CLI integration; JSON output parsing.
- Separate Git worktree for each run.
- Claude Code and Codex only in first executable MVP; later CLI support via adapters.
- No automatic source promotion, self-update, background execution or delegated high-risk approval.
- Test command explicit (argv array) and default pytest; run only on a user-initiated task.

## Next milestone

The owner-selected initial engines are now Codex and Antigravity (`agy`), with default developer/reviewer roles respectively and explicit overrides. Legacy Claude remains compatible. Metadata status is an observer-startup snapshot; it is not a model-call or task-acceptance result. See [engine guide](engines.md).

AO-002: add an office observation UI and verified additional CLI capabilities. Session continuation and unattended operation require separate designs and acceptance checks.
