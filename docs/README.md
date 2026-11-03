# dispatchbox documentation

Start with the [README](../README.md) for what dispatchbox is and a two-minute quick start. Then:

| Page | Read it when you want to… |
|---|---|
| [Rules, events and decisions](rules.md) | write rules: matching, prompts, schedules, decisions, results, retries and notifications |
| [Slack and Feishu](chat.md) | start runs from chat and decide with buttons |
| [Deployment](deployment.md) | run the hub and workers for real: layouts, tokens, start on login, GitHub webhooks |
| [Reference](reference.md) | look up a command-line option, environment variable, HTTP endpoint or run state |
| [Architecture](architecture.md) | understand how the code is put together before changing it |
| [Troubleshooting](troubleshooting.md) | find out why a run is stuck, failed or never started |

Also in the repository:

- [`examples/hello.json`](../examples/hello.json): the quick start rules
- [`rules.example.json`](../rules.example.json): PR review, CI fixes, issue triage, a nightly job, chat requests and a decision follow-up
- [`deploy/`](../deploy): launchd and systemd templates
- [CONTRIBUTING.md](../CONTRIBUTING.md) and [SECURITY.md](../SECURITY.md)

## Roadmap

Ideas the maintainers want to work on, with no dates:

- Slack Socket Mode and Feishu long connections, so the hub needs no public address
- Per-rule stats: duration, failure rate and cost
- An email adapter
