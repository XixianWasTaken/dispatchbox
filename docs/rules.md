# Rules, events and decisions

One JSON file tells the hub what to do with each event. This page covers everything that file can hold, and the conventions an agent follows to report results and ask for decisions.

- [Events](#events)
- [The rules file](#the-rules-file)
- [Rule fields](#rule-fields)
- [Matching and routing](#matching-and-routing)
- [Prompts](#prompts)
- [Schedules](#schedules)
- [Decisions](#decisions)
- [Results](#results)
- [Timeouts, lost runs and retries](#timeouts-lost-runs-and-retries)
- [Notifications](#notifications)
- [Checking a rules file](#checking-a-rules-file)

## Events

Every source produces the same small object:

```json
{
  "source": "github",
  "type": "pr.opened",
  "subject": "me/app#12",
  "summary": "Add login page",
  "link": "https://github.com/me/app/pull/12",
  "payload": { "head_sha": "abc123" },
  "dedupe_key": "optional"
}
```

`source` and `type` are required. Everything else is optional.

If `dedupe_key` is missing, the hub builds one: `source:type:subject` when there is a subject, otherwise a hash of the whole event. An event whose `dedupe_key` was already seen within `dedupe_window` is stored as `duplicate` and starts nothing.

Where events come from:

| Source | `source` | `type` | Dedupe key |
|---|---|---|---|
| `POST /events` or `dispatchbox send` | whatever you send | whatever you send | yours, or the default above |
| GitHub webhook | `github` | see below | `github:<delivery id>` |
| `schedules` in the rules file | `cron` | the schedule name | `cron:<name>:<minute>` |
| Slack | `slack` | `message.mention` or `message.dm` | `slack:<event id>` |
| Feishu | `feishu` | `message.mention` or `message.dm` | `feishu:<message id>` |
| A human decision | `dispatchbox` | `decision.made` | `dispatchbox:decision:<run id>` |

GitHub webhooks are mapped like this. Other GitHub event kinds are acknowledged and ignored.

| GitHub event | `type` | `subject` |
|---|---|---|
| `pull_request` | `pr.<action>`, such as `pr.opened` or `pr.synchronize` | `owner/repo#number` |
| `issues` | `issue.<action>` | `owner/repo#number` |
| `issue_comment`, `pull_request_review_comment` | `comment.<action>` | `owner/repo#number` |
| `push` | `push` | `owner/repo:refs/heads/branch` |
| `workflow_run` (only `completed`) | `ci.<conclusion>`, such as `ci.failure` or `ci.success` | `owner/repo:branch` |

Long text fields in the payload, such as issue bodies, are clipped to 2,000 characters.

## The rules file

```json
{
  "dedupe_window": "10m",
  "memory": ["~/notes/decisions.md"],
  "require_decision_for": ["delete_data", "spend_money", "publish"],
  "defaults": { "worker": "*", "runner": "claude", "timeout": "20m", "notify": "on_decision" },
  "rules": [ ... ],
  "schedules": [ ... ],
  "chat": { ... }
}
```

| Top-level field | Default | What it does |
|---|---|---|
| `rules` | `[]` | The rules, in order. |
| `defaults` | `{}` | Fields every rule inherits unless it sets them itself. |
| `require_decision_for` | `[]` | Kinds of action that need a human first. The list is written into every prompt. With an empty list, the agent is told to ask only when it cannot proceed safely. |
| `memory` | `[]` | Files the agent is told to read before asking anything: decisions you already made, things you already refused. dispatchbox does not read them itself; the agent does. |
| `dedupe_window` | `600` seconds | How long a `dedupe_key` is remembered. |
| `schedules` | `[]` | Cron schedules. See [Schedules](#schedules). |
| `chat` | `{}` | Slack and Feishu settings. See [Chat](chat.md). |

Durations accept a number of seconds or a string such as `90s`, `20m`, `2h` or `1d`.

The hub reloads the file when it changes: on every incoming event and once a minute. If the new file is invalid, the hub logs the error and keeps the last good version.

## Rule fields

```json
{
  "name": "ci-failed",
  "when": { "source": "github", "type": "ci.failure" },
  "worker": "m4",
  "runner": "claude",
  "model": "opus",
  "cwd": "~/code/myapp",
  "prompt": "CI failed on {subject}: {summary}. Find the cause and push a fix to the same branch.",
  "notify": "on_failure",
  "args": ["--permission-mode", "acceptEdits", "--allowed-tools", "Bash(git:*),Bash(gh:*),Bash(npm test:*)"]
}
```

| Field | Default | What it does |
|---|---|---|
| `name` | required | Unique name. Shown on the dashboard and in notifications. |
| `when` | match everything | `source`, `type` (or `types`) and `subject`. Each takes a string or a list, with `*` and `?` wildcards. Missing keys match anything. |
| `continue` | `false` | Keep matching later rules after this one. |
| `worker` | `*` | Pattern for the worker names that may take the run. |
| `runner` | `claude` | `claude`, `codex` or `shell`. |
| `prompt` | required for `claude` and `codex` | The task. See [Prompts](#prompts). |
| `command` | required for `shell` | Run with `/bin/sh -c`. Workers refuse shell runs unless started with `--runners` including `shell`. |
| `cwd` | `.` | Directory the agent runs in, on the worker. `~` is expanded. It must be inside one of the worker's `--allow-dir` directories. |
| `model` | none | Passed as `--model` to Claude or `-m` to Codex. |
| `skill` | none | Adds "Use the `<skill>` skill for this task." to the prompt. |
| `args` | `[]` | Extra arguments for the agent CLI, passed as is. This is where you narrow permissions. |
| `timeout` | `30m` | The worker kills the agent after this long. The run fails with exit code 124. |
| `group` | `{rule}:{subject}` | Runs with the same group never run at the same time. Placeholders allowed. |
| `concurrency` | `1` | How many runs of this rule may run at once, across all workers. |
| `coalesce` | `true` | A new event joins a run of the same rule and group that is still queued, instead of starting another. |
| `notify` | `on_decision` | `never`, `on_decision`, `on_failure` or `always`. See [Notifications](#notifications). |
| `retries` | `0` | How many times to re-queue a run whose worker went silent. See [retries](#timeouts-lost-runs-and-retries). |

What the worker runs for each runner:

| Runner | Command | Prompt |
|---|---|---|
| `claude` | `claude -p --output-format json [--model M] [--resume SESSION] <args>` | stdin |
| `codex` | `codex exec [resume SESSION] --json -o <file> [-m M] <args> -` | stdin |
| `shell` | `/bin/sh -c <command>` | none |

Prompts go through stdin so they do not show up in `ps`. The binaries can be changed with `--claude-bin` and `--codex-bin` on the worker.

## Matching and routing

1. The hub drops the event if its `dedupe_key` was seen within the window.
2. It walks the rules in order. The first match wins, unless that rule has `"continue": true`.
3. For each matched rule it renders the `group`. If `coalesce` is on and a run of the same rule and group is still `queued`, the event joins that run. Otherwise a new run is queued.
4. A worker asking for work gets the oldest queued run whose `worker` pattern matches its name, whose runner it allows, whose group is not already running, and whose rule is below its `concurrency`.

The response to `POST /events` says what happened:

```json
{"event_id": 1, "status": "routed", "runs": [1], "coalesced_into": []}
```

`status` is `routed`, `coalesced`, `duplicate` or `unmatched`.

## Prompts

`prompt` is a template. Placeholders come from the most recent event of the run:

| Placeholder | Value |
|---|---|
| `{source}`, `{type}`, `{subject}`, `{summary}`, `{link}` | Event fields |
| `{payload.a.b}` | A value inside the payload. Objects and lists are inserted as JSON. |
| `{rule}` | The rule name |
| `{event_count}` | How many events were coalesced into the run |

Unknown placeholders are left as they are.

The hub then appends three sections to your prompt:

- **Trigger**: the run number, the rule, every event in the run with its link, and the payloads as JSON (cut at 4,000 characters). For a follow-up run it says which run it continues; for a retry, that the previous attempt may have done part of the work.
- **Standing decisions**: only when `memory` is set. Tells the agent to read those files first.
- **When to ask a human**: the `require_decision_for` list and the line formats below.

The full prompt of every run is on the dashboard.

## Schedules

```json
"schedules": [
  { "name": "nightly-cleanup", "cron": "0 3 * * *", "event": { "summary": "Nightly cleanup" } }
]
```

Each schedule fires an event with `source: cron`, `type` and `subject` set to the schedule name, and anything in `event` merged on top. Write a rule that matches it.

`cron` takes the usual five fields: minute, hour, day of month, month, day of week. Fields accept `*`, numbers, ranges `1-5`, lists `1,15` and steps `*/10`. Day of week counts from 0 for Sunday; 7 also means Sunday. As in classic cron, when both day of month and day of week are restricted, either one matching is enough. Times are in the hub's local time zone.

## Decisions

When the agent reaches an action listed in `require_decision_for`, it is told not to take it and to end its answer with one line:

```
DISPATCHBOX_DECISION: {"question": "Drop the old table x?", "options": ["drop", "keep"], "context": "..."}
```

The run then waits as `awaiting_decision`. You can answer:

- with a button in Slack or Feishu,
- with a button on the run's dashboard page,
- or over the API: `POST /runs/<id>/decision` with `{"choice": "drop", "note": "optional"}` and the bearer token.

A choice must be one of the offered `options` (or any free-text string if `options` is empty `[]`). The run becomes `decided`, and the hub ingests a new event:

```json
{
  "source": "dispatchbox",
  "type": "decision.made",
  "subject": "run-7",
  "payload": { "run_id": 7, "rule": "tidy", "question": "...", "choice": "drop", "note": "", "session_id": "..." }
}
```

Nothing happens next unless a rule matches it. A rule like this one does:

```json
{
  "name": "after-decision",
  "when": { "source": "dispatchbox", "type": "decision.made" },
  "prompt": "The owner chose: {payload.choice}. Carry on from where you stopped.",
  "args": ["--allowed-tools", "Read,Glob,Grep,Bash(rm:*)"]
}
```

The run it creates records the original run as its parent. The worker resumes the parent's agent session with `claude --resume <session>` or `codex exec resume <session>`, so the agent keeps everything it already found.

The follow-up rule can grant more than the first one. In [`examples/hello.json`](../examples/hello.json), the first rule may only read files and the follow-up may also run `rm`. To give different follow-ups to different rules, match on `subject` or write the condition into the prompt using `{payload.rule}`.

## Results

The agent is asked to end every answer with a status and a summary:

```
DISPATCHBOX_STATUS: succeeded
DISPATCHBOX_SUMMARY: Deleted old.log after the owner approved it.
```

The hub decides the run's status like this:

1. If the output contains a valid `DISPATCHBOX_DECISION` line, the run is `awaiting_decision`.
2. Otherwise, if the process exited non-zero, or the agent reported `failed`, the run is `failed`.
3. Otherwise it is `succeeded`.

The second rule matters. An agent stopped by a denied permission usually still exits 0, and only its own status line says the work was not done.

For each run the hub stores the exit code, the summary, the last 8,000 characters of output, the agent session ID, and for Claude the cost in USD and token counts. Codex runs record tokens only.

Worker-side exit codes worth knowing:

| Code | Meaning |
|---|---|
| 124 | Timed out |
| 126 | The rule's `cwd` is outside the worker's `--allow-dir` directories, or does not exist |
| 127 | The agent binary is not installed on the worker |

## Timeouts, lost runs and retries

A running worker sends a heartbeat every 20 seconds. Every 15 seconds the hub marks a run `lost` if it has not heard a heartbeat for 120 seconds, or if it has been running longer than its `timeout` plus 120 seconds.

With `retries` above 0, a lost run is queued again as a new run with the same events. The new prompt says the previous attempt may have done part of the work and asks the agent to check before repeating any step. Retries are off by default for that reason. You are notified about a lost run only once no retry is left.

## Notifications

| `notify` | Notifies when the run… |
|---|---|
| `never` | never |
| `on_decision` | needs a decision |
| `on_failure` | needs a decision, failed or was lost |
| `always` | finishes in any way |

Where notifications go:

- **Slack or Feishu**, when configured. A run that came from a chat message is answered in that conversation, and is not also sent to the notify channel. See [Chat](chat.md).
- **`DISPATCHBOX_NOTIFY_URL`**, if set: a Slack incoming webhook, a Feishu custom bot or any URL that accepts a JSON POST. Plain text with a link, no buttons.
- **The hub log**, as a `NOTIFY` warning, when `DISPATCHBOX_NOTIFY_URL` is not set.

## Checking a rules file

```bash
python3 -m dispatchbox check --rules rules.json
```

```
ok: 7 rules, 1 schedules, dedupe window 600s
```

Add `--source`, `--type` and optionally `--subject` to see which rules an event would match:

```bash
python3 -m dispatchbox check --rules rules.example.json --source github --type ci.failure
```

```
ok: 7 rules, 1 schedules, dedupe window 600s
matches: ci-failed
```
