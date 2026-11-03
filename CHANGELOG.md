# Changelog

All notable changes are recorded here.

## Unreleased

## 0.1.0 - 2026-11-03

First release.

### Added

- Hub with SQLite storage, a JSON rules file that reloads on change, deduplication, coalescing, per-rule concurrency and per-group serialisation.
- Workers that long-poll the hub and run `claude`, `codex` or, when enabled, shell commands, restricted to `--allow-dir` directories.
- Event sources: `POST /events`, GitHub webhooks, cron schedules, Slack and Feishu.
- Decisions: agents ask before the kinds of action listed in `require_decision_for`; answers come from chat buttons, the dashboard or the API, and resume the agent's session.
- Agent-reported results with `DISPATCHBOX_STATUS`, cost and token tracking, and retries for runs whose worker went silent.
- Dashboard, read API, launchd and systemd templates, and `examples/hello.json`.
