# AO-002A local observer results

Checked 2026-11-06. Branch `feature/ao-002-observer`, verified code `92461a2b44d6b66c41599e09423f4dae7972a189`. Stable main remains `585248e`.

## Delivered

- Python loopback-only `serve` command, packaged HTML/CSS/ES-module assets, readonly task/detail/events API.
- Korean task board, recent-200 search/filter and counts, selected task details and seq timeline.
- URL selection preservation, serial polling, hidden-tab pause, stale-response rejection, event dedup/run reset and bounded displayed history.
- Fixed asset map, Host/Origin checks, no CORS, no worker/mutation endpoints; pre/post-read database identity checks.
- Desktop launcher `AI Agent Office - Observer` on this PC. Current local endpoint: `http://127.0.0.1:8765/`.

The preview DB is an explicitly named local snapshot of the completed WSL model-acceptance fixture. It does not follow later WSL source DB changes. The guide describes using an appropriate live local DB instead.

## Verification

| Environment | Result |
|---|---|
| Windows Python 3.11.15 | 111 passed |
| Windows Python 3.13.15 | 111 passed |
| WSL Ubuntu Python 3.11.18 | 110 passed, 1 Windows-only skip |
| WSL Ubuntu Python 3.13.18 | 110 passed, 1 Windows-only skip |
| Windows Node 26.13 / jsdom 31 | 13 state/DOM tests passed |
| WSL native Node 26.13 / jsdom 31 | 13 state/DOM tests passed |
| Installed wheel outside checkout | Four assets and API served; DB hash unchanged |
| Running local endpoint | HTTP 200 for root and selected-task URL, five actual acceptance events returned |

The Node tests include late prior-selection responses, preserved focus during reordering, missing-task recovery, hidden pause, URL-selected task, run change with events failure/recovery and nonduplicated state announcements. They execute the actual app module against a DOM implementation; they are not real browser rendering tests.

CUA inventory reports **Local browser use is disabled in Work settings**. Therefore real desktop/mobile layout, visual focus rings and screen-reader speech quality are not verified. The panel preview request returned queued, not an observed rendered UI. No alternative browser-automation path was used to bypass that setting.

The repository CI definition now includes Node 26/npm ci/frontend tests. No AO-002 remote CI run is claimed before branch publication.

## Actual Codex acceptance

- Windows Codex 0.163: received a real model reply and persisted PLAN_ONLY/five events, but its file tool reported a Core launch installation error. README task acceptance **failed**; no settings/permission bypass was attempted.
- WSL non-login launch initially resolved the Windows npm shim and failed before model execution. Selecting the existing native CLI with a process-local PATH fixed that launch environment without installing Node for the worker.
- WSL native Codex 0.147: read the fixture README and replied `AO002_LIVE_OK:42`; the verification number matched. CLI exit=0, PLAN_ONLY, five events, unchanged source and worktree. The receipt validator accepts whitespace after the colon; no extra model invocation was needed for formatting.
- This establishes one actual WSL read-only Codex acceptance. It does not establish Windows worker file-tool health, Claude authentication or edit-mode acceptance.

## Independent review and fixes

A fresh reviewer checked through `2119f5a` and returned 'with fixes'. Four Important issues were reproduced as failing HTTP/DOM cases and fixed in a single pass:

1. Root `?task=` links returned HTTP400, breaking refresh. Valid single task queries now serve the root document; GET/HEAD regressions pass.
2. New run details could sit beside an old run timeline if new events failed. A run change now synchronously clears the timeline before fetching its events; failure/recovery regression passes.
3. A replaced same-schema database was silently adopted. Server startup pins file identity and checks it before/after reads; replacement now yields503 without exposing replacement rows.
4. Selected status changes and connection restoration were not announced. One polite region now announces transitions/recovery, not unchanged polling.

There was no second independent reviewer approval. The author verified the fixes with RED→GREEN and the full local matrix above.

## Follow-up HTTP fixes

The owner requested completing the PR/CI/merge workflow, and the three deferred findings were addressed before publication:

- Corrupted stored event JSON returns503/STORE_IO rather than blaming the request with400.
- Port80 accepts browser-normalized Host/Origin with or without explicit :80; foreign hosts and ports remain rejected.
- TRACE/CONNECT/other unsupported methods return405 with the fixed JSON error, Allow, no-store and CSP headers.

Seven failing cases demonstrated these defects before the changes; the port guard also retains two foreign-host rejection cases. A subsequent focused review found no Important/Critical issues and identified the505 protocol-error attribution, which was also reproduced RED and corrected to INVALID_ARGUMENT. Original matrix counts above describe the earlier verified candidate; the PR CI records the updated head and final counts.

## Implementation choices

- Used Python stdlib/local assets rather than hosted infrastructure to fit the personal-PC readonly scope. External/multi-user serving would need a separate design.
- Kept API/assets in one complete delivery commit rather than leaving an intermediate root404 server.
- Added jsdom as a development-only dependency because actual browser access is disabled. Runtime Python/package use does not require Node/npm; DOM tests do not prove CSS rendering.
- Retained the loopback observer for the user's manual inspection; it performs no worker actions. The owner's subsequent standing instruction authorizes publishing and CI-gated source merge via AGENTS.md; external hosting/deployment remains outside this scope.

Receipts/test logs are retained privately under `E:\workspace\ai-agent-office-local\ao002-evidence\` and the Linux setup directory. See [observer guide](observer.md), [design](superpowers/specs/2026-11-06-ao-002-observer-design.md) and [implementation plan](superpowers/plans/2026-11-06-ao-002.md).
