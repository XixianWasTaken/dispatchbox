# Contributing to dispatchbox

Thanks for helping. dispatchbox stays small on purpose, so a short read of this page saves everyone time.

## Ground rules

- **Standard library only.** The core must run on a fresh Python 3.10+ with nothing installed. An optional extra is fine when a feature truly needs it, as `cryptography` does for Feishu encryption, but the feature must say clearly what is missing when the package is absent.
- **Thin over clever.** If something can be done by the agent CLI, by a rule or by an existing endpoint, prefer that over new machinery.
- **Safe by default.** New ways in (event sources, chat platforms) verify their requests and start with an empty allowlist. New ways to run things start disabled.
- **Tests with every change.** A bug fix comes with a test that fails without it.

## Development

```bash
git clone https://github.com/XixianWasTaken/dispatchbox.git && cd dispatchbox
python3 -m unittest discover -s tests -t .
```

That runs everything, including end-to-end tests with a fake `claude` and Slack and Feishu tests against a fake API. The Feishu encryption tests are skipped unless `cryptography` is installed:

```bash
uv run --no-project --with cryptography python -m unittest discover -s tests -t .
```

To try a change against a real agent, follow the [quick start](README.md#quick-start) with `examples/hello.json`.

## Where things live

[docs/architecture.md](docs/architecture.md) explains how the pieces fit together.

| File | What it does |
|---|---|
| `dispatchbox/hub.py` | Events, routing, the run lifecycle, decisions, retries, prompt building |
| `dispatchbox/server.py` | HTTP API, webhooks and the dashboard |
| `dispatchbox/worker.py` | Pulls tasks, runs `claude` / `codex` / shell, reports results |
| `dispatchbox/rules.py` | Loads and validates the rules file |
| `dispatchbox/store.py` | SQLite schema and migrations |
| `dispatchbox/chat.py` | Shared chat adapter logic: allowlists, origin tracking, answer text |
| `dispatchbox/slack.py`, `dispatchbox/feishu.py` | Platform adapters |
| `dispatchbox/net.py` | TLS settings for every outgoing HTTPS call |
| `dispatchbox/cron.py` | Five-field cron expressions |

## Adding a chat platform

Subclass `ChatAdapter` in `chat.py` and implement `handle`, `notify` and `run_finished`. Look at `slack.py` for the shape. Your adapter must:

1. Verify every incoming request (signature or token) before doing anything.
2. Turn messages into events with `source` set to the platform name and `type` `message.mention` or `message.dm`, and use a stable ID from the platform (its event or message ID) as the `dedupe_key`, so retried deliveries are ignored.
3. Check `user_allowed` before ingesting and before accepting a decision.
4. Reply in the original conversation in `run_finished`, and stay quiet in `notify` when `from_chat(run)` is true.

Register it in `adapters_from_env` and add tests against the fake API in `tests/test_chat.py`.

## Pull requests

- Keep each PR to one change, and say in the description what it fixes and how you checked it.
- Update the docs when behaviour changes: the page under `docs/` that describes it, and both `README.md` and `README.zh-CN.md` if the README mentions it. If you cannot write one of the languages, say so in the PR and a maintainer will help.
- Add a line under Unreleased in [CHANGELOG.md](CHANGELOG.md) for anything a user would notice.
- CI (`.github/workflows/test.yml`) runs the tests on Linux and macOS with Python 3.10 to 3.14. It must pass.

## Releasing

1. Set the new version in `pyproject.toml` and `dispatchbox/__init__.py`, and move the Unreleased notes in [CHANGELOG.md](CHANGELOG.md) under that version.
2. Commit, then tag and push: `git tag -a vX.Y.Z -m "dispatchbox X.Y.Z" && git push origin main vX.Y.Z`.
3. `.github/workflows/publish.yml` checks that the tag matches the version, runs the tests, builds the package and uploads it to PyPI through trusted publishing. No PyPI token is involved.

## Reporting security issues

Do not open a public issue for something that lets others run code on a worker or bypass an allowlist. See [SECURITY.md](SECURITY.md).
