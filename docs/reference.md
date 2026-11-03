# Reference

- [Command line](#command-line)
- [Environment variables](#environment-variables)
- [HTTP API](#http-api)
- [Agent environment](#agent-environment)
- [Run states](#run-states)

## Command line

Every command runs as `python3 -m dispatchbox <command>`. Add `-v` before the command for debug logging.

### hub

```bash
python3 -m dispatchbox hub [--rules rules.json] [--db dispatchbox.db] [--host 127.0.0.1] [--port 8787] [--public-url URL]
```

| Option | Default | What it does |
|---|---|---|
| `--rules` | `rules.json` | The rules file. See [Rules](rules.md). |
| `--db` | `dispatchbox.db` | SQLite file. Created if missing. |
| `--host` | `127.0.0.1` | Address to listen on. |
| `--port` | `8787` | Port to listen on. |
| `--public-url` | `$DISPATCHBOX_PUBLIC_URL` | The URL people use to reach the hub. Used in notification links. When set, the dashboard always asks for a login. |

### worker

```bash
python3 -m dispatchbox worker [--hub URL] [--name NAME] [--slots 2] [--allow-dir DIR ...] [--runners claude,codex]
```

| Option | Default | What it does |
|---|---|---|
| `--hub` | `$DISPATCHBOX_HUB` or `http://127.0.0.1:8787` | Hub address. |
| `--name` | short host name | Worker name that rules match with `worker`. |
| `--slots` | `2` | Runs at once. |
| `--allow-dir` | current directory | Directories runs may use. Repeatable. A run whose `cwd` is outside all of them fails with exit code 126. |
| `--runners` | `claude,codex` | Runners this worker accepts. Add `shell` to allow shell rules. |
| `--claude-bin` | `claude` | Claude Code binary. |
| `--codex-bin` | `codex` | Codex binary. |

The worker exits at once if `DISPATCHBOX_TOKEN` is not set.

### send

```bash
python3 -m dispatchbox send SOURCE TYPE [--subject S] [--summary S] [--link URL] [--payload JSON] [--hub URL]
```

Sends one event to `POST /events` and prints the hub's answer:

```bash
python3 -m dispatchbox send manual ask --summary "What files are here?"
```

```json
{"event_id": 1, "status": "routed", "runs": [1], "coalesced_into": []}
```

### check

```bash
python3 -m dispatchbox check [--rules rules.json] [--source S --type T [--subject S]]
```

Validates the rules file and, with `--source` and `--type`, prints which rules an event would match. Exits with 1 and prints the error if the file is invalid. See [Checking a rules file](rules.md#checking-a-rules-file).

## Environment variables

| Variable | Used by | What it does |
|---|---|---|
| `DISPATCHBOX_TOKEN` | hub, worker, send | Shared bearer token. Required for workers. The hub generates a temporary one if it is missing. |
| `DISPATCHBOX_HUB` | worker, send | Default for `--hub`. |
| `DISPATCHBOX_PUBLIC_URL` | hub | Default for `--public-url`. |
| `DISPATCHBOX_GITHUB_SECRET` | hub | Enables `POST /hooks/github` and verifies its signatures. |
| `DISPATCHBOX_NOTIFY_URL` | hub | Webhook for notifications. See [Notifications](rules.md#notifications). |
| `DISPATCHBOX_SLACK_BOT_TOKEN`, `DISPATCHBOX_SLACK_SIGNING_SECRET` | hub | Enable Slack. Both or neither. |
| `DISPATCHBOX_FEISHU_APP_ID`, `DISPATCHBOX_FEISHU_APP_SECRET`, `DISPATCHBOX_FEISHU_VERIFICATION_TOKEN` | hub | Enable Feishu. All three or none. |
| `DISPATCHBOX_FEISHU_ENCRYPT_KEY` | hub | Set when the Feishu app has an Encrypt Key. Needs the `cryptography` package. |
| `DISPATCHBOX_SLACK_API`, `DISPATCHBOX_FEISHU_API`, `DISPATCHBOX_FEISHU_TOKEN_URL` | hub | Override the platform API addresses. Used by the tests. |

A half-configured chat platform stops the hub at start with a message naming the missing variable.

## HTTP API

API endpoints take `Authorization: Bearer <DISPATCHBOX_TOKEN>`. Errors come back as `{"error": "..."}` with a 4xx status. For `/runs/...` paths, send `Accept: application/json` to get JSON errors; without it they are rendered as an HTML page.

### Sources

| Method and path | Auth | What it does |
|---|---|---|
| `GET /healthz` | none | Returns `{"ok": true}`. |
| `POST /events` | token | Ingests one [event](rules.md#events). |
| `POST /hooks/github` | GitHub signature | GitHub webhooks. 404 unless `DISPATCHBOX_GITHUB_SECRET` is set. |
| `POST /hooks/slack` | Slack signature | Slack events and button clicks. Present only when Slack is configured. |
| `POST /hooks/feishu` | Feishu token or signature | Feishu events and card callbacks. Present only when Feishu is configured. |

```bash
curl -X POST http://127.0.0.1:8787/events \
  -H "Authorization: Bearer $DISPATCHBOX_TOKEN" -H "Content-Type: application/json" \
  -d '{"source": "manual", "type": "ask", "summary": "What files are here?"}'
```

### Workers

These are what the worker uses. You only need them to write your own worker.

| Method and path | What it does |
|---|---|
| `GET /claim?worker=NAME&runners=claude,codex&wait=50` | Long-polls up to `wait` seconds (at most 60) for a task. Returns 200 with the task, or 204. |
| `POST /runs/{id}/heartbeat` | Body `{"worker": NAME}`. Keeps the run alive. |
| `POST /runs/{id}/result` | Body `{"worker": NAME, "exit_code": 0, "output": "...", "session_id": "...", ...}`. Finishes the run. |

### Decisions

| Method and path | What it does |
|---|---|
| `POST /runs/{id}/decision` | JSON body `{"choice": "...", "note": "..."}` with the token (or form-encoded from the web UI with a `csrf` token). If the decision request defines `options`, `choice` must be one of them; if `options` is empty (`[]`), any free-text `choice` is accepted. Returns 409 if the run is not awaiting a decision, 400 if the choice is not in `options`. |

### Read

| Method and path | What it does |
|---|---|
| `GET /api/runs?status=&limit=100` | Recent runs, newest first. |
| `GET /api/runs/{id}` | One run, with its prompt, output tail, decision and events. |
| `GET /api/events?limit=100` | Recent events. |
| `GET /api/stats` | Run counts by status. |

### Dashboard

| Path | What it shows |
|---|---|
| `/` | Runs, filterable by status. |
| `/runs/{id}` | One run: events, prompt, output, cost, and decision buttons when it is waiting. |
| `/login` | Paste the token to get a session cookie. |

Requests from `127.0.0.1` go straight in, unless they carry proxy headers (`X-Forwarded-For`, `Forwarded` and similar) or the hub has a `--public-url`. Everyone else signs in with the token.

## Agent environment

The worker starts the agent with the worker's own environment, plus:

| Variable | Value |
|---|---|
| `DISPATCHBOX_RUN_ID` | The run number |
| `DISPATCHBOX_RULE` | The rule name |
| `DISPATCHBOX_EVENTS_FILE` | Path to a JSON file with every event of the run. Deleted after the run. |
| `DISPATCHBOX_HUB` | The hub address |

`DISPATCHBOX_TOKEN` is removed, so the agent cannot call the hub's API.

The agent's process group is killed on timeout or when the hub no longer knows the run.

## Run states

| State | Meaning |
|---|---|
| `queued` | Waiting for a worker |
| `running` | A worker took it |
| `succeeded` | Finished, and the agent did not report failure |
| `failed` | Non-zero exit, timeout, or the agent reported `failed` |
| `awaiting_decision` | The agent asked a question |
| `decided` | A human answered; a follow-up run may have started |
| `lost` | The worker stopped reporting |
