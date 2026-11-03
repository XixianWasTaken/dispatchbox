# Deployment

dispatchbox has two processes:

- **The hub** receives events, keeps the queue in a SQLite file and serves the dashboard. Run one.
- **Workers** pull tasks from the hub and run the agents. Run one on each machine that has your code, your agent CLIs and your signed-in accounts.

Workers only make outbound HTTP requests to the hub, so a laptop behind NAT works as a worker.

- [Layouts](#layouts)
- [The token](#the-token)
- [Running the hub](#running-the-hub)
- [Running a worker](#running-a-worker)
- [Start on login: macOS](#start-on-login-macos)
- [Start on login: Linux](#start-on-login-linux)
- [GitHub webhooks](#github-webhooks)
- [Backups and upgrades](#backups-and-upgrades)

## Layouts

**One machine.** Hub and worker on the same computer, the hub listening on `127.0.0.1`. Enough for HTTP events, cron and `dispatchbox send`. No public address is needed.

**Hub on a server, workers at home.** Put the hub on a small server behind HTTPS (a reverse proxy such as Caddy or nginx in front of `127.0.0.1:8787`). Point workers at it with `--hub https://<your hub>`. This is the layout you need for GitHub, Slack and Feishu webhooks, since they must reach the hub from the internet.

**Hub at home behind a tunnel.** Same as above, with a tunnel such as `cloudflared` instead of a server.

The hub listens on `127.0.0.1` by default. Pass `--host 0.0.0.0` only when something in front of it handles TLS, or on a network you trust.

## The token

Everything that talks to the hub uses one bearer token, `DISPATCHBOX_TOKEN`: workers, `dispatchbox send`, `POST /events`, the read API and the dashboard login.

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

If the hub starts without `DISPATCHBOX_TOKEN`, it generates one for that process and prints it in the log. That is fine for a test and wrong for anything else, because the token changes on every restart.

The token lets its holder run agents on your workers. Treat it like an SSH key. See [SECURITY.md](../SECURITY.md).

## Running the hub

```bash
export DISPATCHBOX_TOKEN=...
python3 -m dispatchbox hub --rules rules.json --db dispatchbox.db --public-url https://<your hub>
```

`--public-url` is used in links inside notifications and chat cards. Setting it also makes the dashboard ask for a login on every request, including from localhost. See [Reference](reference.md#hub) for all options.

The hub writes one SQLite file (plus its `-wal` and `-shm` files) and logs to stderr.

## Running a worker

```bash
export DISPATCHBOX_TOKEN=...
python3 -m dispatchbox worker --hub https://<your hub> --name m4 --slots 2 --allow-dir ~/code
```

- `--name` is what rules match with `worker`. It defaults to the machine's short host name.
- `--slots` is how many runs the worker takes at once.
- `--allow-dir` limits where runs may happen. Repeat it for several directories. Without it, only the directory the worker was started in is allowed.
- `--runners` lists the runners this worker accepts. The default is `claude,codex`; add `shell` to allow shell rules.

The worker needs `claude` and/or `codex` on its `PATH`, signed in as the user the worker runs as. A worker that cannot find the binary fails the run with exit code 127.

## Start on login: macOS

Fill in the templates in `deploy/launchd/` and load them. The worker template sets `PATH`, because launchd's minimal default would not find `claude` or `codex`. The generated files contain your token, so they are made readable only by you.

```bash
export DISPATCHBOX_TOKEN=<your token>
for f in hub worker; do
  sed -e "s#__DISPATCHBOX_DIR__#$PWD#g" -e "s#__HOME__#$HOME#g" -e "s#__CODE_DIR__#$HOME/code#g" \
      -e "s#__TOKEN__#$DISPATCHBOX_TOKEN#g" deploy/launchd/dev.dispatchbox.$f.plist > ~/Library/LaunchAgents/dev.dispatchbox.$f.plist
  chmod 600 ~/Library/LaunchAgents/dev.dispatchbox.$f.plist
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.dispatchbox.$f.plist
done
```

Run this from the repository root. The hub template expects `rules.json` there. Logs go to `~/Library/Logs/dispatchbox-hub.log` and `~/Library/Logs/dispatchbox-worker.log`.

To stop and remove them:

```bash
for f in hub worker; do launchctl bootout gui/$(id -u)/dev.dispatchbox.$f; done
```

## Start on login: Linux

The user units in `deploy/systemd/` expect the repository at `~/dispatchbox` and code under `~/code`. Edit the paths if yours differ.

```bash
mkdir -p ~/.config/dispatchbox ~/.config/systemd/user
echo "DISPATCHBOX_TOKEN=<your token>" > ~/.config/dispatchbox/env && chmod 600 ~/.config/dispatchbox/env
cp deploy/systemd/dispatchbox-*.service ~/.config/systemd/user/
systemctl --user enable --now dispatchbox-hub dispatchbox-worker
```

If the hub runs on another machine, add `DISPATCHBOX_HUB=https://<your hub>` to the env file and enable only the worker. To keep user units running after you log out, run `loginctl enable-linger $USER`.

## GitHub webhooks

1. Generate a secret and start the hub with it:

   ```bash
   export DISPATCHBOX_GITHUB_SECRET="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
   ```

   Without this variable, `/hooks/github` answers 404.
2. In the repository or organization settings, add a webhook:
   - **Payload URL**: `https://<your hub>/hooks/github`
   - **Content type**: `application/json`
   - **Secret**: the same value
   - **Events**: the ones your rules use, such as pull requests, issues, issue comments, pushes and workflow runs.
3. GitHub sends a `ping` first. The hub answers it once the signature checks out.

See [Events](rules.md#events) for how each GitHub event is mapped.

## Backups and upgrades

All state is in the SQLite file given by `--db`. Stop the hub before copying it, or use `sqlite3 dispatchbox.db ".backup backup.db"`.

dispatchbox has no releases yet. To upgrade, pull the repository and restart the hub and workers. The hub adds missing database columns on start.
