# Changelog

All notable changes are recorded here. dispatchbox has not been released yet, so everything is under Unreleased. The package metadata says `0.1.0`, but no version has been tagged or published.

## Unreleased

### Added

- Hub with SQLite storage, a JSON rules file that reloads on change, deduplication, coalescing, per-rule concurrency and per-group serialisation.
- Workers that long-poll the hub and run `claude`, `codex` or, when enabled, shell commands, restricted to `--allow-dir` directories.
- Event sources: `POST /events`, GitHub webhooks, cron schedules, Slack and Feishu.
- Decisions: agents ask before the kinds of action listed in `require_decision_for`; answers come from chat buttons, the dashboard or the API, and resume the agent's session.
- Agent-reported results with `DISPATCHBOX_STATUS`, cost and token tracking, and retries for runs whose worker went silent.
- Dashboard, read API, launchd and systemd templates, and `examples/hello.json`.

### Fixed

- Requests through a tunnel or reverse proxy are no longer treated as local by the dashboard.
- Changes to chat `allowed_users` apply to the next message instead of up to a minute later.
- A run started in one chat platform no longer also posts a decision card to the other.
- Chat replies show the agent's answer without the `DISPATCHBOX_*` marker lines.
- Outgoing HTTPS falls back to `certifi` on Python builds without CA certificates.

### Changed

- The project was renamed from baton to dispatchbox. Environment variables, the Python package, the marker lines and the `source` of decision events changed with it (`BATON_*` → `DISPATCHBOX_*`, `baton/decision.made` → `dispatchbox/decision.made`).
