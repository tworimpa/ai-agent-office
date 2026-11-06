# Local observer

Run from an installed Agent Office environment with an existing AO-001 database:

```text
agent-office --db /path/to/tasks.sqlite3 serve --port 8765
```

Open the printed `http://127.0.0.1:8765/` URL in a local browser. `--db` precedes the command. Use a candidate DB for development and a different port from other running observers. Port 0 chooses a free local port. Ctrl+C stops the server; it does not cancel a worker.

The observer reads recent 200 tasks, task details and ordered phase events. Search and status filtering apply to that recent window. Selecting a task stores its ID in the page URL, so refresh restores that task. A direct task URL can show an older task outside the recent list. The timeline keeps the latest 1,000 displayed entries while its cursor continues. Polling pauses in hidden tabs; connection errors mark the last data stale.

The browser does not edit the DB. Viewing/refreshing never executes workers, recovers jobs, cancels a process, creates a database or changes CLI authentication. `PLAN_ONLY` means a response was produced without tests/review; it does not establish successful completion of the requested real-world task. In particular, a model can report a blocker in its response despite a successful process exit.

No separate Node server or npm install is needed to run the observer. Static assets are installed in the Python package. Node 26 and `npm ci` are needed only for frontend development/DOM tests. Runtime Python dependencies are unchanged.

This personal-PC server binds only 127.0.0.1 and offers fixed read-only routes. Host/Origin checks, no CORS, CSP, no-store, a fixed asset map and fixed error messages limit its HTTP surface. It is not a remotely hosted or multi-user service. Do not expose it through a reverse proxy or network forwarding as an authentication substitute.

The server pins the selected database file identity. Replacing the DB while it is running requires stopping/restarting the observer; it will not silently switch to the new file. See [candidate results](ao-002-results.md) for verified environments, browser-check limits and the follow-up HTTP fixes.

## Live CLI acceptance

Windows Codex 0.163 returned a blocker from its file tool (Core launch requires an installed app); task acceptance did not pass. The response and five journal events were received, which proves communication/journaling, not file access.

WSL's first non-login process found the Windows npm shim and failed before model execution. With a process-local PATH selecting the already-installed native Codex 0.147, a separate readonly fixture run read the verification number from README and returned `AO002_LIVE_OK:42`. The recorded state was PLAN_ONLY with five ordered events and unchanged source/worktree. This is a real read-only Codex acceptance on WSL, not a Claude or write-mode acceptance.

Receipts are retained locally under the setup/evidence directories. The Windows preview may use an explicitly named snapshot copy of that completed WSL acceptance DB. Such a snapshot does not follow subsequent changes to the original WSL DB; point `--db` at the appropriate local database for ongoing work. Never share a live SQLite database across hosts.
