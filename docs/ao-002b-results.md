# Codex / Antigravity initial profile results

Owner selected Codex and Antigravity as the initial engines. Default roles are Codex developer and Antigravity reviewer; either can be selected for another role. Legacy Claude and existing task records remain compatible.

Verified code before documentation-only updates: `fbdb3ecf65f71b111ee6fdd0c44f650b0bac33cc`. Stable base: `b73731b`. No live model request, new login, credential-file inspection or persistent provider permission setting change was performed for this work.

## Implementation and verification

- Shared engine registry drives CLI defaults, adapter selection, store validation and event allowlist without changing schema1.
- Antigravity uses official agy stdin NDJSON with plan/accept-edits, sandbox and disabled slash expansion. Terminal success is checked independently of process exit0; interrupted/timeout remains recovery-required.
- Metadata-only CLI and startup snapshot separate installed/version/auth/headless flags/actual execution. Repeated API requests do not run probes or models.
- Two semantic engine cards show configured default roles and startup diagnostic time. Their cached API is independent of DB metadata availability.
- Local Windows Python3.11: **154 passed**; Node state/DOM tests: **16 passed**. The PR CI is the authoritative latest-head Windows/Linux3.11/3.13 matrix receipt after publication.
- Actual metadata after safe PATH resolution: Codex0.163.0 authenticated metadata; Antigravity1.5.2 installed/headless flags advertised, auth unknown. Earlier inspection found agy1.4.2; the later version changed independently of this implementation.
- Both engines retain actual-execution=not_checked for this host. Earlier WSL Codex acceptance and Windows Core file-tool failure do not become new provider acceptance automatically.

## Independent review and fixes

A fresh reviewer found three Important issues at `86789e7`: conflicting/mixed result envelopes and records after a supposed terminal result could be accepted; Windows cwd lookup could launch a repository-local same-name program; a failed DB metadata request could prevent engine cards loading. All were reproduced as RED cases, corrected and verified in the complete suite.

The parser now distinguishes plain vs event envelopes, requires a terminal result to be last and rejects malformed reported tool errors. NDJSON separates only on LF, retaining U+2028 inside valid JSON text. Executable resolution uses explicit absolute PATH entries excluding cwd/relative/blank entries, preserving Windows PATHEXT behavior while ignoring Unix extensionless shims there. Safe resolution is shared by adapters, doctor and metadata. UI loads the snapshot even during a fresh-page DB outage.

Provider identities in live-verification documentation were also corrected to Codex/Antigravity. There was no second independent reviewer approval; the fixes are evidenced by RED→GREEN and full-suite results.

## Real browser checks

The first sandbox-hosted candidate timed out from the browser. A permitted PC-local loopback candidate at `http://127.0.0.1:8766/` was reachable without changing browser or provider security settings.

Using the supported CUA browser, the two cards, current metadata labels, five fixture events, search-empty/restored state filter, task selection preserved across reload and keyboard Tab from search to filter were observed. Desktop1280 and mobile390 widths had document scrollWidth no larger than viewport width. Screenshots were saved as local `engine-status-desktop.jpg` and `engine-status-mobile.jpg` task artifacts. These observations replace the previous browser-access limitation only for these tested flows; screen-reader speech quality and all device/browser combinations were not measured.

The candidate displays a readonly snapshot of the earlier WSL fixture. It is not a new Antigravity live run or evidence of editing acceptance. Current production/stable processes are not redeployed by the source merge.

References: [engine guide](engines.md), [design](superpowers/specs/2026-11-06-codex-antigravity-design.md), [plan](superpowers/plans/2026-11-06-codex-antigravity.md), [official protocol](https://antigravity.google/docs/cli/headless/).
