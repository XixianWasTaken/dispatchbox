"""HTTP layer for the hub: JSON API for sources and workers, GitHub webhook adapter, and a small web UI."""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import logging
import secrets
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .hub import Hub, HubError

log = logging.getLogger("dispatchbox.server")

MAX_BODY = 2 * 1024 * 1024
PROXY_HEADERS = ("X-Forwarded-For", "X-Real-IP", "Forwarded", "CF-Connecting-IP", "X-Forwarded-Host")
MAX_WAIT = 60.0


class DispatchboxServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], hub: Hub, token: str, github_secret: str = ""):
        super().__init__(addr, Handler)
        self.hub = hub
        self.token = token
        self.github_secret = github_secret
        self.ui_cookie = hmac.new(token.encode(), b"dispatchbox-ui", hashlib.sha256).hexdigest()
        self.csrf = hmac.new(token.encode(), b"dispatchbox-csrf", hashlib.sha256).hexdigest()
        self._stop = threading.Event()

    def start_background(self) -> None:
        def loop() -> None:
            last_reap = 0.0
            while not self._stop.is_set():
                try:
                    self.hub.tick_cron()
                    if time.monotonic() - last_reap > 15:
                        self.hub.reap()
                        last_reap = time.monotonic()
                except Exception:
                    log.exception("background loop error")
                self._stop.wait(1.0)

        threading.Thread(target=loop, name="dispatchbox-bg", daemon=True).start()

    def shutdown(self) -> None:
        self._stop.set()
        super().shutdown()


class Handler(BaseHTTPRequestHandler):
    server: DispatchboxServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("%s %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------------ helpers

    @property
    def hub(self) -> Hub:
        return self.server.hub

    def _send(self, status: int, body: bytes = b"", ctype: str = "application/json", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith(("text/", "application/json")) else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, obj: Any) -> None:
        self._send(status, json.dumps(obj, ensure_ascii=False, default=str).encode())

    def _html(self, status: int, page: str, headers: dict[str, str] | None = None) -> None:
        self._send(status, page.encode(), "text/html", headers)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise HubError(413, "body too large")
        return self.rfile.read(n) if n else b""

    def _json_body(self) -> dict[str, Any]:
        raw = self._body()
        try:
            obj = json.loads(raw or b"{}")
        except json.JSONDecodeError as e:
            raise HubError(400, f"invalid JSON: {e}") from e
        if not isinstance(obj, dict):
            raise HubError(400, "body must be a JSON object")
        return obj

    def _api_authed(self) -> bool:
        auth = self.headers.get("Authorization", "")
        return auth.startswith("Bearer ") and hmac.compare_digest(auth[7:].strip(), self.server.token)

    def _is_loopback(self) -> bool:
        """A direct connection from this machine, not one relayed by a tunnel or reverse proxy.

        Tunnels (cloudflared, ngrok) and proxies connect from 127.0.0.1 too, so a loopback address alone
        proves nothing. They add forwarding headers; a remote client adding them only makes us stricter.
        When the hub has a public URL it is meant to be reached from outside, so loopback is never trusted.
        """
        if self.hub.public_url:
            return False
        if any(self.headers.get(h) for h in PROXY_HEADERS):
            return False
        return self.client_address[0] in {"127.0.0.1", "::1", "::ffff:127.0.0.1"}

    def _ui_authed(self) -> bool:
        if self._is_loopback() or self._api_authed():
            return True
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "dispatchbox_ui" and hmac.compare_digest(v, self.server.ui_cookie):
                return True
        return False

    def _require_api(self) -> None:
        if not self._api_authed():
            raise HubError(401, "missing or wrong bearer token")

    # ------------------------------------------------------------------ dispatch

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        try:
            self._route(method, parts, query)
        except HubError as e:
            if parts[:1] in (["api"], ["events"], ["claim"], ["hooks"]) or self.headers.get("Accept", "").startswith("application/json"):
                self._json(e.status, {"error": str(e)})
            else:
                self._html(e.status, page("Error", f"<p>{html.escape(str(e))}</p><p><a href='/'>Back</a></p>"))
        except BrokenPipeError:
            pass
        except Exception as e:
            log.exception("request failed")
            self._json(500, {"error": f"internal error: {e.__class__.__name__}"})

    def _route(self, method: str, parts: list[str], q: dict[str, str]) -> None:
        if parts == ["healthz"]:
            return self._json(200, {"ok": True})

        # ---- sources
        if method == "POST" and parts == ["events"]:
            self._require_api()
            return self._json(200, self.hub.ingest(self._json_body()))
        if method == "POST" and parts == ["hooks", "github"]:
            return self._github()
        if method == "POST" and len(parts) == 2 and parts[0] == "hooks" and parts[1] in self.hub.adapters:
            body = self._body()
            headers = {k.lower(): v for k, v in self.headers.items()}
            status, extra, payload = self.hub.adapters[parts[1]].handle(headers, body)
            return self._send(status, payload, extra.pop("Content-Type", "application/json"), extra)

        # ---- workers
        if method == "GET" and parts == ["claim"]:
            self._require_api()
            wait = min(float(q.get("wait", 0) or 0), MAX_WAIT)
            runners = q["runners"].split(",") if q.get("runners") else None
            task = self.hub.claim(q.get("worker", ""), wait=wait, runners=runners)
            return self._send(204) if task is None else self._json(200, task)
        if method == "POST" and len(parts) == 3 and parts[0] == "runs" and parts[2] in {"heartbeat", "result"}:
            self._require_api()
            body = self._json_body()
            run_id = _int(parts[1])
            if parts[2] == "heartbeat":
                self.hub.heartbeat(run_id, str(body.get("worker", "")))
                return self._json(200, {"ok": True})
            return self._json(200, self.hub.finish(run_id, str(body.get("worker", "")), body))

        # ---- decisions (API or UI form)
        if method == "POST" and len(parts) == 3 and parts[0] == "runs" and parts[2] == "decision":
            run_id = _int(parts[1])
            if self.headers.get("Content-Type", "").startswith("application/json"):
                self._require_api()
                body = self._json_body()
                return self._json(200, self.hub.decide(run_id, str(body.get("choice", "")), str(body.get("note", "")),
                                                       by=str(body.get("by", "api"))))
            if not self._ui_authed():
                raise HubError(401, "sign in first")
            form = {k: v[-1] for k, v in parse_qs(self._body().decode()).items()}
            if not hmac.compare_digest(form.get("csrf", ""), self.server.csrf):
                raise HubError(403, "bad form token, reload the page")
            self.hub.decide(run_id, form.get("choice", ""), form.get("note", ""), by="web")
            return self._send(303, headers={"Location": f"/runs/{run_id}"})

        # ---- read API
        if method == "GET" and parts[:1] == ["api"]:
            self._require_api()
            if parts == ["api", "runs"]:
                return self._json(200, self.hub.list_runs(_int(q.get("limit", "100")), q.get("status")))
            if len(parts) == 3 and parts[1] == "runs":
                run = self.hub.get_run(_int(parts[2]))
                return self._json(200, run) if run else self._json(404, {"error": "not found"})
            if parts == ["api", "events"]:
                return self._json(200, self.hub.list_events(_int(q.get("limit", "100"))))
            if parts == ["api", "stats"]:
                return self._json(200, self.hub.stats())

        # ---- web UI
        if parts == ["login"]:
            return self._login(method)
        if method == "GET" and parts in ([], ["runs"]):
            if not self._ui_authed():
                return self._send(303, headers={"Location": "/login"})
            return self._html(200, render_index(self.hub, q.get("status")))
        if method == "GET" and len(parts) == 2 and parts[0] == "runs":
            if not self._ui_authed():
                return self._send(303, headers={"Location": "/login"})
            run = self.hub.get_run(_int(parts[1]))
            if not run:
                raise HubError(404, "run not found")
            return self._html(200, render_run(run, self.server.csrf))

        raise HubError(404, "not found")

    def _login(self, method: str) -> None:
        if method == "POST":
            form = {k: v[-1] for k, v in parse_qs(self._body().decode()).items()}
            if hmac.compare_digest(form.get("token", ""), self.server.token):
                cookie = f"dispatchbox_ui={self.server.ui_cookie}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000"
                return self._send(303, headers={"Location": "/", "Set-Cookie": cookie})
            return self._html(401, page("Sign in", LOGIN_FORM + "<p class='err'>Wrong token.</p>"))
        return self._html(200, page("Sign in", LOGIN_FORM))

    def _github(self) -> None:
        body = self._body()
        secret = self.server.github_secret
        if not secret:
            raise HubError(404, "GitHub webhook is not configured (set DISPATCHBOX_GITHUB_SECRET)")
        sig = self.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise HubError(401, "bad GitHub signature")
        kind = self.headers.get("X-GitHub-Event", "")
        if kind == "ping":
            return self._json(200, {"ok": True, "pong": True})
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as e:
            raise HubError(400, "invalid JSON") from e
        event = github_event(kind, payload, self.headers.get("X-GitHub-Delivery", ""))
        if event is None:
            return self._json(200, {"ignored": kind})
        return self._json(200, self.hub.ingest(event))


def _int(s: str) -> int:
    try:
        return int(s)
    except (TypeError, ValueError) as e:
        raise HubError(400, f"not a number: {s!r}") from e


# ---------------------------------------------------------------------- GitHub adapter

def _clip(s: Any, n: int = 2000) -> str:
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[:n] + "..."


def github_event(kind: str, p: dict[str, Any], delivery: str) -> dict[str, Any] | None:
    """Turn a GitHub webhook into a dispatchbox event. Returns None for kinds we don't map."""
    repo = (p.get("repository") or {}).get("full_name", "")
    action = p.get("action", "")
    sender = (p.get("sender") or {}).get("login", "")
    base = {"source": "github", "payload": {"repo": repo, "action": action, "sender": sender}}
    if delivery:
        base["dedupe_key"] = f"github:{delivery}"

    if kind == "pull_request":
        pr = p.get("pull_request") or {}
        base["payload"].update({"number": pr.get("number"), "title": pr.get("title"), "body": _clip(pr.get("body")),
                                "head_ref": (pr.get("head") or {}).get("ref"), "head_sha": (pr.get("head") or {}).get("sha"),
                                "base_ref": (pr.get("base") or {}).get("ref"), "draft": pr.get("draft"),
                                "merged": pr.get("merged")})
        return {**base, "type": f"pr.{action}", "subject": f"{repo}#{pr.get('number')}",
                "summary": _clip(pr.get("title"), 200), "link": pr.get("html_url", "")}
    if kind == "issues":
        issue = p.get("issue") or {}
        base["payload"].update({"number": issue.get("number"), "title": issue.get("title"), "body": _clip(issue.get("body")),
                                "labels": [l.get("name") for l in issue.get("labels") or []]})
        return {**base, "type": f"issue.{action}", "subject": f"{repo}#{issue.get('number')}",
                "summary": _clip(issue.get("title"), 200), "link": issue.get("html_url", "")}
    if kind in {"issue_comment", "pull_request_review_comment"}:
        issue = p.get("issue") or p.get("pull_request") or {}
        c = p.get("comment") or {}
        base["payload"].update({"number": issue.get("number"), "comment": _clip(c.get("body")),
                                "is_pr": "pull_request" in issue or kind == "pull_request_review_comment"})
        return {**base, "type": f"comment.{action}", "subject": f"{repo}#{issue.get('number')}",
                "summary": _clip(c.get("body"), 200), "link": c.get("html_url", "")}
    if kind == "push":
        commits = p.get("commits") or []
        base["payload"].update({"ref": p.get("ref"), "after": p.get("after"),
                                "commits": [_clip(c.get("message"), 200) for c in commits[:20]]})
        return {**base, "type": "push", "subject": f"{repo}:{p.get('ref', '')}",
                "summary": f"{len(commits)} commit(s) by {sender}", "link": p.get("compare", "")}
    if kind == "workflow_run":
        run = p.get("workflow_run") or {}
        if action != "completed":
            return None
        base["payload"].update({"workflow": run.get("name"), "conclusion": run.get("conclusion"),
                                "head_branch": run.get("head_branch"), "head_sha": run.get("head_sha")})
        return {**base, "type": f"ci.{run.get('conclusion')}", "subject": f"{repo}:{run.get('head_branch')}",
                "summary": f"{run.get('name')} {run.get('conclusion')}", "link": run.get("html_url", "")}
    return None


# ---------------------------------------------------------------------- web UI

STYLE = """
:root{--bg:#fafaf9;--fg:#1c1917;--muted:#78716c;--line:#e7e5e4;--card:#fff;--accent:#2563eb;
--ok:#15803d;--bad:#b91c1c;--wait:#b45309;--run:#2563eb}
@media (prefers-color-scheme:dark){:root{--bg:#0c0a09;--fg:#e7e5e4;--muted:#a8a29e;--line:#292524;--card:#1c1917;
--accent:#60a5fa;--ok:#4ade80;--bad:#f87171;--wait:#fbbf24;--run:#60a5fa}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,system-ui,sans-serif}
main{max-width:1000px;margin:0 auto;padding:24px 16px}a{color:var(--accent);text-decoration:none}
h1{font-size:20px;margin:0 0 16px}h2{font-size:15px;margin:24px 0 8px}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}.chip{padding:4px 10px;border:1px solid var(--line);border-radius:999px;background:var(--card)}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:500}
.s{font-weight:600}.s-succeeded{color:var(--ok)}.s-failed,.s-lost{color:var(--bad)}.s-awaiting_decision{color:var(--wait)}
.s-running,.s-queued{color:var(--run)}.s-decided{color:var(--muted)}
.muted{color:var(--muted)}pre{white-space:pre-wrap;word-break:break-word;background:var(--card);border:1px solid var(--line);
border-radius:8px;padding:12px;max-height:420px;overflow:auto;font-size:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px}
button{font:inherit;padding:8px 14px;border-radius:6px;border:1px solid var(--accent);background:var(--accent);color:#fff;cursor:pointer;margin:4px 8px 4px 0}
input{font:inherit;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);width:100%;max-width:420px}
.err{color:var(--bad)}.scroll{overflow-x:auto}
"""

LOGIN_FORM = """<form method="post" action="/login" class="card"><p>Paste the hub token to open the dashboard.</p>
<input type="password" name="token" autofocus><p><button>Sign in</button></p></form>"""


def page(title: str, body: str, refresh: int = 0) -> str:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return (f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"{meta}<title>{html.escape(title)} · dispatchbox</title><style>{STYLE}</style></head><body><main>{body}</main></body></html>")


def _ts(t: float | None) -> str:
    return datetime.fromtimestamp(t).strftime("%m-%d %H:%M:%S") if t else ""


def _dur(run: dict[str, Any]) -> str:
    if run.get("claimed_at") and run.get("finished_at"):
        s = int(run["finished_at"] - run["claimed_at"])
        return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"
    return ""


def _cost(run: dict[str, Any]) -> str:
    return "" if run.get("cost_usd") is None else f"${run['cost_usd']:.4f}"


def _usage(run: dict[str, Any]) -> str:
    parts = []
    if run.get("cost_usd") is not None:
        parts.append(_cost(run))
    if run.get("input_tokens") is not None or run.get("output_tokens") is not None:
        parts.append(f"{run.get('input_tokens') or 0:,} in / {run.get('output_tokens') or 0:,} out")
    return " · ".join(parts)


def render_index(hub: Hub, status: str | None) -> str:
    stats = hub.stats()
    chips = "".join(
        f"<a class='chip' href='/?status={html.escape(k)}'><span class='s s-{html.escape(k)}'>{html.escape(k)}</span> {v}</a>"
        for k, v in sorted(stats.items())
    )
    chips = f"<a class='chip' href='/'>all</a>{chips}"
    rows = []
    for r in hub.list_runs(200, status):
        rows.append(
            f"<tr><td><a href='/runs/{r['id']}'>#{r['id']}</a></td><td>{html.escape(r['rule'])}</td>"
            f"<td class='s s-{r['status']}'>{html.escape(r['status'])}</td><td>{html.escape(r.get('claimed_by') or '')}</td>"
            f"<td class='muted'>{_ts(r['created_at'])}</td><td class='muted'>{_dur(r)}</td>"
            f"<td class='muted'>{_cost(r)}</td>"
            f"<td>{html.escape((r.get('summary') or '')[:140])}</td></tr>"
        )
    table = ("<div class='scroll'><table><tr><th>Run</th><th>Rule</th><th>Status</th><th>Worker</th><th>Created</th>"
             "<th>Took</th><th>Cost</th><th>Summary</th></tr>" + "".join(rows) + "</table></div>") if rows else "<p class='muted'>No runs yet.</p>"
    events = hub.list_events(30)
    ev_rows = "".join(
        f"<tr><td>{e['id']}</td><td class='muted'>{_ts(e['received_at'])}</td><td>{html.escape(e['source'])}/{html.escape(e['type'])}</td>"
        f"<td>{html.escape(e['subject'])}</td><td>{html.escape(e['status'])}</td><td class='muted'>{html.escape(e['note'])}</td></tr>"
        for e in events
    )
    ev_table = ("<div class='scroll'><table><tr><th>#</th><th>Received</th><th>Event</th><th>Subject</th><th>Status</th><th></th></tr>"
                + ev_rows + "</table></div>") if events else "<p class='muted'>No events yet.</p>"
    return page("Runs", f"<h1>dispatchbox</h1><div class='chips'>{chips}</div>{table}<h2>Recent events</h2>{ev_table}", refresh=10)


def render_run(run: dict[str, Any], csrf: str) -> str:
    esc = html.escape
    spec = run["spec"]
    meta = [
        ("Status", f"<span class='s s-{run['status']}'>{esc(run['status'])}</span>"),
        ("Rule", esc(run["rule"])), ("Runner", esc(f"{spec.get('runner')} {spec.get('model') or ''}")),
        ("Worker", esc(run.get("claimed_by") or "")), ("Directory", esc(spec.get("cwd") or "")),
        ("Group", esc(run["group_key"])), ("Created", _ts(run["created_at"])), ("Took", _dur(run)),
        ("Exit code", esc(str(run.get("exit_code") if run.get("exit_code") is not None else ""))),
        ("Session", esc(run.get("session_id") or "")),
        ("Usage", esc(_usage(run))),
        ("Attempt", esc(str(run.get("attempt") or 1))),
    ]
    if run.get("retry_of"):
        meta.append(("Retry of", f"<a href='/runs/{run['retry_of']}'>#{run['retry_of']}</a>"))
    if run.get("parent_run"):
        meta.append(("Continues", f"<a href='/runs/{run['parent_run']}'>#{run['parent_run']}</a>"))
    meta_html = "<table>" + "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in meta) + "</table>"

    decision_html = ""
    req = run.get("decision_request")
    if req:
        opts = req.get("options") or []
        if run["status"] == "awaiting_decision":
            buttons = "".join(f"<button name='choice' value='{esc(o, quote=True)}'>{esc(o)}</button>" for o in opts)
            free = "" if opts else "<p><input name='choice' placeholder='Your decision'></p><button>Send</button>"
            decision_html = (
                f"<h2>Needs your decision</h2><div class='card'><p><b>{esc(req.get('question', ''))}</b></p>"
                f"<p class='muted'>{esc(req.get('context', '') or '')}</p>"
                f"<form method='post' action='/runs/{run['id']}/decision'><input type='hidden' name='csrf' value='{csrf}'>"
                f"<p><input name='note' placeholder='Optional note for the agent'></p>{buttons}{free}</form></div>"
            )
        else:
            d = run.get("decision") or {}
            decision_html = (f"<h2>Decision</h2><div class='card'><p>{esc(req.get('question', ''))}</p>"
                             f"<p><b>{esc(str(d.get('choice', '')))}</b> <span class='muted'>{esc(str(d.get('note', '')))}</span></p></div>")

    events = "".join(
        f"<li>[{esc(e['source'])}/{esc(e['type'])}] {esc(e['subject'])}: {esc(e['summary'])}"
        + (f" <a href='{esc(e['link'], quote=True)}' rel='noreferrer'>open</a>" if e.get("link", "").startswith(("http://", "https://")) else "")
        + "</li>"
        for e in run.get("events", [])
    )
    body = (
        f"<p><a href='/'>← all runs</a></p><h1>Run #{run['id']}</h1>"
        f"<p>{esc(run.get('summary') or '')}</p>{decision_html}<h2>Details</h2>{meta_html}"
        f"<h2>Events</h2><ul>{events}</ul>"
        f"<h2>Prompt</h2><pre>{esc(run.get('prompt') or '(not claimed yet)')}</pre>"
        f"<h2>Output (tail)</h2><pre>{esc(run.get('output_tail') or '')}</pre>"
    )
    refresh = 5 if run["status"] in {"queued", "running"} else 0
    return page(f"Run #{run['id']}", body, refresh)


def serve(hub: Hub, host: str, port: int, token: str | None, github_secret: str = "") -> DispatchboxServer:
    if not token:
        token = secrets.token_urlsafe(24)
        log.warning("No DISPATCHBOX_TOKEN set. Generated one for this process: %s", token)
    srv = DispatchboxServer((host, port), hub, token, github_secret)
    srv.start_background()
    return srv
