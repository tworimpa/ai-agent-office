# Initial engines: Codex and Antigravity

Owner selection: Codex + Antigravity. Default Developer=codex, Reviewer=antigravity. Roles can be swapped using --developer/--reviewer. Legacy Claude adapter and stored task records remain supported when explicitly selected.

```text
agent-office engines
agent-office doctor --require codex antigravity
agent-office --db ../candidate/tasks.sqlite3 run --repo . --task "Propose one small improvement without modifying files"
```

The first two commands inspect metadata only. The run command calls the provider and consumes its plan/API usage. Its default is planning-only; it does not execute tests or a reviewer. Editing still requires --allow-edits in an appropriately isolated disposable worktree; provider permission modes are not interchangeable OS sandbox guarantees.

## Antigravity transport

The adapter calls official `agy`, not the IDE executable. It sends a single stdin NDJSON user message and expects one final result. Both plan/accept-edits modes retain --sandbox and --disable-slash-commands. No skip-permissions flag, persistent settings edit, new login or credentials injection is used.

The [official headless protocol](https://antigravity.google/docs/cli/headless/) defines status and response in the terminal result. The adapter fails closed for non-SUCCESS, malformed/missing/multiple results, empty/nonstring response, error fields or reported tool errors even if process exit is0. INTERRUPTED/timeout require recovery review. This deliberately rejects some recoverable tool-error runs instead of automatically accepting an incomplete review. It does not parse provider responses as human permission approval.

## Status snapshot

The observer collects known fixed version/help commands and Codex login status once at startup. GET /api/engines only serves that cached snapshot; opening or refreshing a page never performs model calls or repeats CLI probes. Timestamp/platform and 'startup diagnostic' labels make freshness explicit. Restart the observer to recollect, or run engines in a terminal for a fresh independent report.

No credential file or environment inventory is read. Only extracted semantic versions and authentication enums are exposed, not raw output, account details, executable paths or tokens. Antigravity help provides no standalone authentication-status command in the inspected CLI, so its login state remains unknown. IDE login and an executable being installed are not evidence of CLI authentication or successful file/tool execution.

Vendor executable resolution uses explicit absolute PATH entries and excludes the current directory, blank and relative entries. A same-name launcher placed in the viewed repository is not implicitly selected. The status snapshot is fetched independently of database metadata so it remains visible if the DB becomes unavailable after server startup.

The initial Windows probe found Codex0.163.0 with authenticated metadata and Antigravity1.4.2 with advertised headless flags; a later probe found agy1.5.2. Subsequent owner-authorized real checks passed Windows Antigravity reading and a WSL Codex0.147.0 + isolated Antigravity1.5.2 edit/test/review fixture. Windows Codex file-tool execution still failed. The installed WSL agy1.1.13 was not the tested 1.5.2 binary, so default WSL PATH readiness is not established. See [real acceptance evidence and limits](ao-002b-live-acceptance.md). These results do not automatically change cached engine-card metadata.

Real acceptance must be separately authorized and use a bounded fixture with file/result checks. `-Live`/`--live` verification scripts are opt-in provider communication checks; inspect the returned task response and use independent evidence for task success. They are not run by CI or by engine-card polling.
