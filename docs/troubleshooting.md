# Troubleshooting

Start with the hub log and the run's page on the dashboard. The page shows the full prompt, the output tail, the exit code and the summary.

| Symptom | Cause and fix |
|---|---|
| `send` prints `401` | The token in this terminal differs from the hub's. Export the same `DISPATCHBOX_TOKEN` in every terminal. |
| `send` prints `"status": "unmatched"` | No rule matches the event's `source` and `type`. Check with `python3 -m dispatchbox check --rules <file> --source <s> --type <t>`. |
| `send` prints `"status": "duplicate"` | The same `dedupe_key` arrived within `dedupe_window`. Change the subject, set a distinct `dedupe_key`, or wait. |
| A run stays `queued` | No worker can take it. Check that a worker is running, that its `--name` matches the rule's `worker`, that its `--runners` includes the rule's runner, and that no run of the same group is still running. |
| A run fails with exit code 126 | The rule's `cwd` is outside the worker's `--allow-dir` directories, or does not exist on that machine. |
| A run fails with exit code 127 | The worker cannot find `claude` or `codex`. Under launchd or systemd, check the `PATH` in the service file. |
| A run fails with exit code 124 | It hit its `timeout`. |
| A run `failed` with exit code 0 | The agent reported `DISPATCHBOX_STATUS: failed`, often because a permission was denied. Its output says which. Widen the rule's `args`. |
| A run is `lost` | The worker stopped sending heartbeats: it was stopped, crashed or lost its network. Set `retries` if the task is safe to repeat. |
| Nothing happens after a decision | No rule matches `source: dispatchbox`, `type: decision.made`. Add one; see [Decisions](rules.md#decisions). |
| The follow-up run starts a new conversation instead of resuming | The first run recorded no session ID. Check that the runner is `claude` or `codex` and that its output was valid JSON. |
| The hub refuses to start with "half configured" | Some but not all chat variables are set. Set the missing one or unset the others. |
| Slack shows no **Verified** after you enter the Request URL | The hub is not reachable at that URL, or it started without the signing secret. Check that `https://<your hub>/healthz` returns `{"ok": true}` and that the hub log says `chat: slack`. |
| Feishu says the challenge got no response, and the hub logs `verification token mismatch` | The Verification Token in your environment is not the one in the console, often because the Encrypt Key or App Secret was pasted instead. Copy it again from Encryption Strategy and restart the hub. |
| The bot replies "You are not allowed to start dispatchbox runs" | Add the user ID from that reply to `allowed_users`. The change applies to the next message. |
| A decision button answers "You are not allowed to decide dispatchbox runs." | Your user ID is not in `approvers` (or in `allowed_users`, if `approvers` is not set). |
| `CERTIFICATE_VERIFY_FAILED` in the logs | This Python has no CA certificates. dispatchbox falls back to the `certifi` package when it is installed: `pip install certifi`. With the python.org installer on macOS, you can instead run `Install Certificates.command` in the Python folder. |
| The dashboard asks for a login on localhost | The hub has `--public-url` set, or the request came through a proxy or tunnel. Paste the token. |
