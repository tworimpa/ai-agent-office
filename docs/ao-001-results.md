# AO-001 candidate implementation results

Checked 2026-11-06. Branch: `feature/ao-001-task-store`.
Verified code: `d3dab9b03ed640f013c4552965be48a75dbe50d0`; stable base: `c82482e2326ef6619cf6b15c15ad73793438389c`.
Subsequent documentation-only commits do not change the verified code.

## Implemented

- SQLite tasks/runs/events, atomic transition and event commits, idempotent operation envelopes, ordered cursor queries.
- Allowlisted phase metadata, bounded UTF-8 request snapshots and known credential-pattern masking.
- Noninherited per-run OS locks and explicit interrupted-run marking without worker/Git/test replay.
- Journaled Developer → Test → Reviewer flow; strict planning-only, failure, timeout and approval state distinctions.
- `--db`, `tasks list/show`, `events`, `recover`, fixed JSON errors, stable/candidate paths and Windows interpreter fallback.

## Final local matrix

| OS | Python | Result |
|---|---|---|
| Windows | 3.11.15 | 85 passed |
| Windows | 3.13.15 | 85 passed |
| WSL Ubuntu 26.04 | 3.11.18 | 84 passed, 1 skipped |
| WSL Ubuntu 26.04 | 3.13.18 | 84 passed, 1 skipped |

The Linux skip is the Windows PowerShell interpreter-resolution test. All four use actual OS-native subprocesses/file locks. compileall, CLI help and doctor --require succeeded. These are local matrix results, not GitHub Actions results. Provider credentials/model calls were not used.

Actual coordinator termination is tested after committed intent, worktree creation, worker start and completion before journal commit. Recovery does not increase worker invocation count. Transaction failures prevent the next external stage. Korean Git repository paths, Unicode database paths and candidate/stable database separation are tested.

Logs are preserved locally under `E:\workspace\ai-agent-office-local\ao001-evidence\` and `/home/sh-cat-lee/.local/share/agent-office-setup/ao001-bootstrap/pytest-final-*.log`. Raw DBs/worker output are not published.

## Independent review and fix evidence

A fresh read-only reviewer evaluated c82482e through pre-fix 6b5dada and returned **No, fix required**. The author added 13 failing regression cases, observed all fail, fixed the findings and ran the complete final matrix above. There was no second independent approval; the fix evidence is RED→GREEN and the full suites.

Fixed findings:

1. Quoted JSON credential names and escaped quotes could leak known secrets into snapshots. Regression cases reopen the DB and inspect stored bytes.
2. Adapter truncation could omit a contradictory trailing review verdict. Truncated reviews now require human review.
3. Hard-link aliases could bypass path-based ownership locks. Multiply hard-linked DB files are rejected at store/guard entry.
4. Recovery could initialize existing empty/unrecognized databases. Existing-only SQLite mode prevents initialization/recreation.
5. View-only foreign DBs and malformed same-version schemas were accepted. User objects and required managed schema definitions are now checked.
6. Windows cp949 decoding broke Korean Git roots. Git output decoding is explicit UTF-8.
7. Empty state environment variables selected CWD-relative paths. Empty values now use home defaults.
8. Oversized SQLite integers and explicit empty run IDs broke JSON error semantics. They are rejected before DB access.

## Decisions and limits

- Integer overflow/empty-ID failures were regraded from Minor to Important because machine clients cannot rely on traceback output or a silently substituted run. SQLite signed integer bounds are enforced; oversized requests fail as INVALID_ARGUMENT.
- Actual provider authentication, output quality and edit-mode acceptance remain separately authorized validation. Fake-worker results do not establish them.
- Single-host/local-disk support remains the agreed scope. Shared/network DB use is unsupported; local hard-link aliases are explicitly rejected.
- Arbitrary unknown prose secrets, total subprocess output-memory bounds and long-term DB quotas remain deferred. This candidate does not claim unattended readiness.
- Remote CI awaits publication approval. No source push, new PR, merge or deployment was performed for this candidate.

## Candidate locations

Windows: `E:\workspace\ai-agent-office-agent-worktrees\ao-001-plan` with its own `.venv`.
Linux: `/home/sh-cat-lee/workspaces/ai-agent-office-ao001-candidate`.
Stable Windows/WSL checkouts remain on main at c82482e.

See [storage guide](storage.md) and [acceptance map](verification.md#ao-001-candidate-acceptance-map).
