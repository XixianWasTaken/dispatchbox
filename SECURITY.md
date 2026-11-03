# Security

dispatchbox starts coding agents on your machines in response to events from the network. This page explains what it protects against, what it leaves to you, and how to report a problem.

## Reporting a vulnerability

Please do not open a public issue for a security problem. Use GitHub's private vulnerability reporting on this repository (the **Security** tab, then **Report a vulnerability**). If that option is not available, open an issue asking for a private contact without including any details.

This is a small project maintained in spare time. There is no guaranteed response time, and there is no bug bounty.

Supported version: only the latest commit on the default branch. There are no releases yet.

## What dispatchbox protects

| Entry point | Protection |
|---|---|
| `POST /events`, worker endpoints, read API | Bearer token, compared in constant time |
| `POST /hooks/github` | HMAC-SHA256 signature with `DISPATCHBOX_GITHUB_SECRET`; disabled when the secret is not set |
| `POST /hooks/slack` | Slack signing secret, and timestamps more than five minutes off are rejected |
| `POST /hooks/feishu` | Verification Token, or decryption and signature check when an Encrypt Key is set |
| Chat messages and buttons | `allowed_users` to start runs and `approvers` to decide; both are empty by default |
| Dashboard | Token login with an HMAC-derived cookie and a CSRF token on decision forms. Requests from `127.0.0.1` skip the login, unless they carry proxy headers or the hub has `--public-url` set |
| Worker | Runs only inside `--allow-dir` directories; the shell runner is off unless enabled; the agent's environment does not contain `DISPATCHBOX_TOKEN`; prompts go through stdin, not the command line |

## What it does not protect

- **The agent itself.** dispatchbox does not sandbox agents. An agent can do anything its CLI permissions and your user account allow. Narrow every rule with `--allowed-tools` (Claude) or `-s read-only` / `-s workspace-write` (Codex) in `args`.
- **Prompt injection.** Issue bodies, PR descriptions, comments and chat messages go into prompts. Anyone who can open an issue on a public repository can write text your agent will read. Give rules fed by untrusted sources read-only permissions, and keep risky kinds of action in `require_decision_for`.
- **A leaked token.** Whoever has `DISPATCHBOX_TOKEN` can queue runs and claim tasks. There is one token for everything, and no per-worker or per-user tokens.
- **Transport.** The hub serves plain HTTP. Put it behind a TLS-terminating proxy or tunnel before it leaves your machine.
- **Data at rest.** The SQLite file holds prompts, event payloads and agent output, unencrypted. Protect it like the repositories it describes.
- **Availability.** There is no rate limiting. A client with the token, or a flood of signed webhooks, can fill the queue.

## Recommendations

1. Generate a long random token and keep it out of version control. The launchd and systemd examples store it in files readable only by you.
2. Keep the hub on `127.0.0.1` and expose it through HTTPS only when you need webhooks.
3. Set `--public-url` whenever the hub is reachable from outside, so the dashboard always asks for a login.
4. Give each rule the narrowest permissions that work, and widen them only in the follow-up rule after a decision.
5. Allowlist chat users by ID. Avoid `"*"` in workspaces with guests.
6. Run workers as a user that has access only to what the agents need.
