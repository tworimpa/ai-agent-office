# Codex + Antigravity real-provider acceptance

Checked on 2026-11-06 against merged source `d5f939e90ab0c9c04eb48de46b1f3114c0c41329` (PR #5). The owner explicitly authorized the read checks and one disposable edit/test/review attempt. This result is separate from mock CI and metadata-only engine cards.

## Result by environment

| Environment and runtime | Actual file read | Actual edit/test/review |
| --- | --- | --- |
| Windows Codex npm 0.163.0 | Failed: process exited 0 but reported local execution tools could not start; random README marker was not returned | Not run |
| Windows app-bundled Codex 0.167.0-alpha.3.2 | Same semantic failure with the explicit installed executable | Not run |
| Windows Antigravity 1.5.2 | Passed: exact random README marker; checkout unchanged | Not run |
| WSL Ubuntu-26.04 Codex 0.147.0 + isolated Antigravity 1.5.2 | Both passed: exact random README marker; checkout unchanged | Passed: one attempt, tests passed, reviewer approved |

The WSL result used the existing Codex executable and an official Antigravity 1.5.2 Linux binary downloaded into the disposable acceptance folder. The installed WSL `agy` was 1.1.13 and lacked `--input-format`, so it was not used for this run. The release archive SHA512 matched the official vendor manifest. A harness subclass changed only Antigravity's executable path to that absolute binary; production adapter flags, stdin framing, result parsing and the coordinator graph were unchanged. Neither installed CLI nor PATH/profile settings were replaced. **This does not establish that the current default WSL PATH pair is ready**, or fix Windows Codex.

## Disposable edit acceptance

Task ID: `f29bcb3cf1b0412db871e8ba5a1c2db2`

Run ID: `ec14f0735f354ac4a2305b56adf2a941`

Developer: Codex; reviewer: Antigravity; maximum attempts: 1; per-provider timeout: 180 seconds.

The committed fixture started with `add(a, b)` returning `a - b`. Its two assertions (`add(2, 3) == 5`, `add(-2, 1) == -1`) failed before development. The coordinator created its own Git worktree. Codex changed only `calculator.py` to return `a + b`. The coordinator test command printed `FIXTURE_TESTS_PASSED` and exited 0; an independent repeat of that fixture test also exited 0. Antigravity returned the standalone marker `VERDICT: APPROVED`. The coordinator returned `READY_FOR_HUMAN_REVIEW`, outcome `APPROVED`, attempts 1 and exit 0.

Independent Git inspection confirmed the expected one-line diff and a clean original fixture checkout. The fixture change was not committed, pushed, merged into the project or deployed. Worker prompts prohibited credential/environment inspection, network use, subagents, Git mutations, publication and permission changes; those prompt constraints are not themselves an OS sandbox guarantee.

The SQLite journal contained exactly nine ordered events:

```text
1 RUN_CREATED
2 WORKSPACE_READY
3 STAGE_STARTED    developer / codex
4 STAGE_COMPLETED  developer / codex / exit 0
5 STAGE_STARTED    test
6 STAGE_COMPLETED  test / exit 0
7 STAGE_STARTED    reviewer / antigravity
8 STAGE_COMPLETED  reviewer / antigravity / exit 0
9 RUN_FINISHED     READY_FOR_HUMAN_REVIEW
```

The browser showed the same task, one attempt, `APPROVED`, and all nine timeline records in a separate read-only observer on loopback port 8767. That observer ran on Windows, so its engine cards correctly remained Windows startup metadata with actual-execution `not_checked`; they did not become WSL acceptance indicators.

## Evidence and limits

The local chat workspace retains `wsl-pair-20261106-203428/summary.json`, `run-report.json`, `events.json`, `acceptance.sqlite3`, `release.json`, `checksum-verification.json` (expected/actual SHA512 and comparison), the original fixture and its worktree. The separate Windows read evidence is in `live-pair-20261106-201934-df76d839/summary.json`. `wsl_edit_acceptance.py` records the one successful coordinator invocation; an earlier harness command had an argparse error before a run or model editing call was created. No automatic retry or repeated edit attempt was used.

This is a small bounded fixture acceptance, not a general reliability, process-isolation, cancellation, recovery, cost or production-release qualification. Windows Codex's file-tool launch failure remains unresolved. Native diagnostic metadata noted incomplete/outdated elevated sandbox provisioning, but that observation does not establish the cause. No sandbox provisioning, security-policy modification, credential-file access or unrestricted bypass was attempted.

A subsequent task must either resolve Windows Codex through its supported runtime setup or explicitly configure and verify a durable WSL runtime selection. The isolated WSL executable override is evidence for this run, not a silently installed routing feature. Runtime-aware acceptance evidence in engine cards is also future work.

Official references: [Antigravity headless protocol](https://antigravity.google/docs/cli/headless/), [official installation](https://antigravity.google/docs/getting-started?tab=cli).
