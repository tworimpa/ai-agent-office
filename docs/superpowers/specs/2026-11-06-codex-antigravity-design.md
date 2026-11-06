# AO-002B Codex / Antigravity initial engines

Owner selection: start with Codex and Antigravity. Default Developer=codex, Reviewer=antigravity; roles remain selectable. Existing Claude adapter/data remain compatible but are not part of the initial profile. Standing AGENTS workflow applies through PR, latest CI and merge.

## Adapter contract

Use the official Antigravity CLI `agy`, not its IDE launcher. Local Windows `agy --version` returned1.4.2 and help advertised the required flags. Official [headless documentation](https://antigravity.google/docs/cli/headless/) describes stdin NDJSON user messages and terminal result envelopes.

The adapter sends exactly one NDJSON user message through stdin, closes stdin and waits with a bounded timeout. argv uses `--input-format stream-json --output-format stream-json --mode plan|accept-edits --sandbox --disable-slash-commands --print-timeout <seconds>s`. Prompts are not argv/shell strings. No permission bypass or persistent settings change is added. Editing mode still requires the existing explicit --allow-edits option.

A valid result is exactly one final `event=result` with a dict result (or a single plain JSON result envelope), status SUCCESS, a nonempty string response and no error. ERROR/CANCELED/WAITING/INVALID/RUNNING, missing/multiple results, malformed JSON and reported tool errors fail closed even when process exit is0. INTERRUPTED maps to returncode124/recovery-required; external timeout retains existing124 behavior. Other nonzero process exits stay failures. Session ID is parsed but not persisted in phase events. Truncated reviewer responses still require human review.

Shared engine constants live in engines.py. Adapter, store engine validation and event allowlist consume the same values. Schema1 is unchanged; adding a valid engine does not rewrite existing records. Legacy tests explicitly select their original Claude/Codex pair, while a separate test proves the new defaults through the journal.

## Status catalog

`agent-office engines` collects only bounded known metadata commands: fixed --version/--help, and Codex login status. It never reads credential files or dumps environment values and never invokes a model. Version output is reduced to a semantic version; auth output is reduced to enums, not echoed. Installed, process/version probe, headless flag support, auth and real execution verification are distinct fields. Failures/timeouts never mean authenticated or usable.

Codex auth is authenticated/not_authenticated/unknown using official login-status output. The inspected agy help exposes no standalone auth-status command, so Antigravity auth remains unknown rather than inferred from files or IDE login. No --print/slash/model request is used as an authentication probe. Real execution is not_checked on this host until an explicitly authorized acceptance; metadata and mocks never become live PASS.

Initial catalog contains exactly codex and antigravity. CLI doctor defaults to that profile and resolves antigravity to agy; legacy --require claude remains supported. Default run roles change to codex/antigravity and can be overridden explicitly.

Observer startup collects an immutable status snapshot once (injectable in tests). `/api/engines` serves that snapshot with existing Host/Origin/no-store/CSP protections. GET/polling performs no subprocess probes. The snapshot includes checked_at and host platform, not executable/home paths or raw diagnostics. Re-diagnosis requires restarting the observer or invoking engines in the terminal; UI labels clearly say startup snapshot. Engine status remains available independently of a later DB error.

## UI

Two semantic engine cards precede the task board: engine name/default role, installed/version, auth state, headless protocol support and actual execution=unverified. Unknown auth is '로그인 미확인', not '미로그인'. No generic 'ready' badge conflates these levels. Show snapshot time and platform. No login, verify/model or mutation button is added.

Polling may fetch cached `/api/engines`; existing task selection/timeline/focus/stale behavior remains. Reuse the existing accessibility, textContent and responsive-layout patterns. Card rendering is stable when the snapshot is unchanged and does not insert controls into the reading order unexpectedly.

## Acceptance

- Fake native subprocess tests assert exact stdin/argv, no bypass, timeout, valid SUCCESS and all fail-closed statuses; missing CLI returns127.
- Store/journal and event metadata accept Antigravity while legacy records/tests remain valid. Default roles are tested with fake providers, no model billing.
- Metadata tests cover missing/failed/timed-out/unsupported tools, authenticated/unknown output, raw-secret suppression and an Antigravity auth probe count of0.
- HTTP snapshot tests prove repeated GET does not rerun metadata commands or workers. DOM tests prove two cards and unknown/authenticated/unverified distinctions without HTML interpretation.
- Windows/Linux Python matrix and Node tests remain required; independent review then CI-gated merge under owner standing authorization.
- Existing Windows Core tool error, WSL Codex acceptance, browser visual QA limits and Antigravity real-run/auth uncertainty remain explicitly separate. No additional paid/live acceptance or permission expansion is silently performed.
