# AO-000 verification — Windows + Linux

## Scope / exact meaning of PASS

1. **Unit + mock subprocess CI:** Python dependencies resolve; graph tests, Git worktree tests, CLI command construction, parsing, native subprocess stdin handling pass. No real LLM call.
2. **Authenticated smoke (local PC only):** Installed Claude Code and Codex run in headless read-only/planning modes, return a result, and leave a separate worktree. Requires local CLI login and service access.
3. **Write-mode acceptance (manual permission):** With explicit `--allow-edits`, a CLI can edit a disposable worktree and run local tests; no auto merge, push or deployment.
4. **OS:** GitHub Actions tests cover Ubuntu + Windows independently. User machine execution remains a separate acceptance gate.

**No provider token belongs in a GitHub Actions matrix job**. Mock workers cannot prove authentication, model service availability, file access sandboxing, or actual CLI version compatibility.

## Prerequisites

- Windows 11 PowerShell 5.1+ or Linux shell
- Python 3.11 or 3.13 (CI); local Python 3.11+ supported
- Git, access to GitHub, package installation from PyPI
- For authenticated smoke only: locally installed `claude` and `codex`, logged in using their official CLI flows
- Use a non-sensitive clone on an account without privileged access

## Clone PR branch

```bash
git clone https://github.com/tworimpa/ai-agent-office.git
cd ai-agent-office
git switch feature/ao-000-bootstrap
```

## Windows

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\verify-windows.ps1
# Optional only after installing and authenticating both CLIs:
powershell -ExecutionPolicy Bypass -File .\scripts\verify-windows.ps1 -Live
```

## Linux

```bash
bash scripts/verify-linux.sh
# Optional, needs authenticated CLIs and may consume API/plan usage:
bash scripts/verify-linux.sh --live
```

The scripts install dependencies, compile sources, run pytest, inspect CLI executables. `-Live` / `--live` additionally invokes Claude Code and Codex read-only runs. The scripts do **not** auto-install or authenticate proprietary CLIs.

## Explicit edit-mode manual acceptance

Only on a **disposable clone** with no secrets, run:

```bash
agent-office run --repo . --developer codex --reviewer claude --allow-edits --max-attempts 1 --task "Add a tiny unit test that asserts 1 + 1 == 2; do not change any other code."
```

Confirm: task worktree is created outside original checkout, new test is present, tests pass, reviewer only reads, `main` remains unchanged. Inspect `git diff` in the reported worktree. Do not merge automatically. On Windows, verify the installed Codex sandbox really permits writes inside the worktree; never use a full-access bypass to overcome a broken sandbox.

## Result recording template

| OS | Python | Git | Claude CLI | Codex CLI | pytest | read-only live | isolated write | outcome |
|---|---|---|---|---|---|---|---|---|
| Ubuntu GitHub Actions | 3.11/3.13 | CI | not installed | not installed | pending | N/A | mock only | pending |
| Windows GitHub Actions | 3.11/3.13 | CI | not installed | not installed | pending | N/A | mock only | pending |
| Windows user machine | user environment | required | user setup | user setup | pending | pending | pending | pending |
| Linux user machine | user environment | required | user setup | user setup | pending | pending | pending | pending |

## Troubleshooting

- `claude` or `codex` not found: run `agent-office doctor`, check PATH and install via vendor's instructions.
- Git worktree failure: ensure the repo has at least one commit and Git executable is available.
- Windows subprocess launcher failure: update the CLI, ensure official launcher on PATH, repeat read-only smoke; task prompts are sent over stdin.
- Codex Windows workspace-write blocked: check installed version and Windows native sandbox settings; do not use unrestricted permissions. A read-only smoke can still validate session and communication.
- Tests failed: save Python version, traceback, and `git status`; do not merge.
- Timeouts and quota/auth issues: distinguish provider failures from orchestrator failures. Live checks require valid provider access.

## Release gate

PR #1 was merged on explicit user instruction at c82482e. Authenticated provider smoke and isolated edit acceptance remain separate, unverified gates. Human approval is required for subsequent merge or deployment.

## AO-001 candidate acceptance map

The new candidate is verified without provider credentials. Actual model execution and write-mode provider acceptance remain unverified.

| Acceptance | Test evidence |
|---|---|
| A01/A02 | test_run_is_durable_with_exact_stage_sequence: separate Git worktree, persisted state, exact five/nine events |
| A03/A04/A05 | test_cli_retry_and_failure_outcomes; conflicting/multiple verdict regression cases |
| A06 | test_timeout_never_retries_even_with_budget: developer/test/reviewer timeout, no extra calls |
| A07/A08 | test_cli_storage_failure_reports_error_without_success; test_journal_failure_stops_external_execution |
| A09/A10 | test_live_owner_is_protected_and_dead_owner_recovery_is_idempotent; four actual coordinator crash windows; query/recover CLI |
| A11 | test_same_operation_is_idempotent_and_changed_target_rejected; rollback/concurrent sequence tests |
| A12 | test_request_snapshot_masks_known_tokens_and_caps_utf8; Unicode/space database path |
| A13 | missing DB/schema/invalid transition/query argument cases |
| A14 | candidate default path/explicit path tests; doctor no DB access; stable separation regression |

Final local matrix versions/results and review findings are recorded in docs/ao-001-results.md when verified. The repository CI still defines Windows/Ubuntu × Python 3.11/3.13; a local matrix pass does not establish a new GitHub Actions run.
