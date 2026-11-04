# Changelog

All notable changes are recorded here.

## Unreleased

## 0.1.3 - 2026-11-04

### Fixed

- A run that continued an agent session after a decision recorded the session's running total, so the cost of the earlier runs (Claude) or their token counts (Codex) were counted twice. Each run now records only its own share.
- Claude Code with `verbose` on, by flag or in its settings, prints its JSON output as one array on a single line. The worker took that for raw output and lost the result, the session ID and the cost. It now reads the result out of the array.

## 0.1.2 - 2026-11-03

### Fixed

- API calls to `/runs/...` got an HTML error page unless they sent `Accept: application/json`. Requests with a bearer token or a JSON body now always get JSON errors.

## 0.1.1 - 2026-11-03

### Fixed

- Resuming a Codex session after a decision failed when the follow-up rule's `args` contained `-s` or `--sandbox`, which `codex exec resume` does not accept. The worker now passes the sandbox as `-c sandbox_mode="..."` when resuming.

### Added

- `examples/hello-codex.json`, the quick start rules for Codex.

## 0.1.0 - 2026-11-03

First release.

### Added

- Hub with SQLite storage, a JSON rules file that reloads on change, deduplication, coalescing, per-rule concurrency and per-group serialisation.
- Workers that long-poll the hub and run `claude`, `codex` or, when enabled, shell commands, restricted to `--allow-dir` directories.
- Event sources: `POST /events`, GitHub webhooks, cron schedules, Slack and Feishu.
- Decisions: agents ask before the kinds of action listed in `require_decision_for`; answers come from chat buttons, the dashboard or the API, and resume the agent's session.
- Agent-reported results with `DISPATCHBOX_STATUS`, cost and token tracking, and retries for runs whose worker went silent.
- Dashboard, read API, launchd and systemd templates, and `examples/hello.json`.
