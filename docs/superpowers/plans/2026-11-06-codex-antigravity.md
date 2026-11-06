# Codex and Antigravity Initial Engines Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Implement inline with TDD and one final independent branch review.

**Goal:** Codex와 Antigravity를 초기 엔진으로 구성하고 실제로 확인한 설치/인증/프로토콜 상태를 화면에 분리 표시한다.
**Architecture:** 공유 engine constants, strict agy stdin adapter, metadata-only catalog and immutable observer snapshot. 기존 CLI/SQLite/journal를 재사용한다.
**Tech Stack:** Python3.11+, existing LangGraph/sqlite/http server, existing JS/jsdom/Node tests.
**Spec:** [설계](../specs/2026-11-06-codex-antigravity-design.md). Owner's explicit two-engine selection and existing observer scope apply.

## Global Constraints

- DEFAULT_DEVELOPER=codex, DEFAULT_REVIEWER=antigravity, INITIAL_ENGINES=(codex,antigravity). Legacy claude remains explicit optional.
- No credential-file or broad environment inspection. No live/model/slash requests as metadata/auth probes.
- No permission bypass, persistent vendor setting changes or invented auth/verified status.
- Schema1 retained; engine validation shared by adapter/store/events.
- Snapshot GET executes no subprocess; startup probes are bounded fixed commands. Raw stdout/stderr/path/user account data is not exposed.
- Existing Windows/Linux Python3.11/3.13 + Node CI retained; normal push/PR/guarded merge under AGENTS standing authorization.

## Review Focus

1. agy process exit0 but terminal ERROR/missing/multiple/permission-error result treated as success.
2. Adding Antigravity to one allowlist but not store/event/CLI serialization.
3. Metadata generation reading/printing credentials or launching an agent/model on a page poll.
4. Installed/auth/protocol availability conflated with actual worker success.
5. Initial role changes silently rewriting existing tasks or invalidating legacy explicit engine use.

## Task 1: Shared profile and Antigravity adapter

Create `src/agent_office/engines.py`; modify adapters/store/events/cli and tests.
Contracts: ENGINE_COMMANDS maps public engine IDs to executable names, SUPPORTED_ENGINES includes legacy Claude. `parse_antigravity(output: str)->AgentResult`; CLIAdapter.command/execute retains original API. Antigravity stdin encodes one user message, not argv prompt.

- [ ] RED: argv/staging mode/no bypass/stdin transport, valid JSON and NDJSON success, each non-success status, malformed/multiple result, tool errors, timeout/missing binary.
- [ ] Implement constants/adapter and shared store/event engine validation. Set initial role defaults. Update old scenario tests to explicitly use their original pair; add default-pair end-to-end fake-provider journal test.
- [ ] Full pytest GREEN and commit.

## Task 2: Metadata catalog and cached API

Create `src/agent_office/engine_status.py`; modify diagnostics/CLI/observer and tests.
Contracts: `collect_engine_status(timeout: int=5)->dict` with checked_at/platform/engines. Each entry exposes engine/command/label/default_role/installed/version/probe_status/auth_status/headless_support/execution_status(not_checked). `ObserverServer(...,engine_snapshot:dict|None=None)` collects once if not injected. `GET /api/engines` returns the startup snapshot; `agent-office engines` prints a fresh snapshot without DB/model calls.

- [ ] RED: missing/failed/timed-out tools, strict safe version and auth normalization, fixed probe argv, Antigravity auth unknown/no auth subprocess, no raw-secret leakage.
- [ ] RED: repeated API GET does not recollect/probe, no worker calls or DB mutation; new CLI command does not open a DB. Tests inject a snapshot to avoid using host CLIs.
- [ ] Implement, run full pytest, commit.

## Task 3: Initial engine cards, documentation and integration

Modify web HTML/CSS/app and DOM tests, README/verification scripts; add `docs/engines.md` and result evidence.

- [ ] RED: two semantic cards, auth unknown distinct from not_authenticated, actual execution unverified even if metadata ready, text-only rendering, unchanged task/timeline interaction.
- [ ] Implement cached status rendering with snapshot time/platform, default roles, no login/model buttons. Preserve empty/error/task selection behavior.
- [ ] Python/Node tests and independent branch review; Important findings RED→GREEN. No real-provider usage or private credentials to make tests pass.
- [ ] Publish reviewable PR, wait latest-head CI, resolve blockers, expected-head merge and clean local-main verification. Preserve dirty work and report actual verification limits.
