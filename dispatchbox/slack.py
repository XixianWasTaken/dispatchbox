"""Slack adapter over plain HTTP (Events API + Interactivity), standard library only.

Formats follow docs.slack.dev: v0 request signing, chat.postMessage / chat.update, Block Kit
buttons, block_actions payloads, app_mention and message.im events.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import time
from typing import Any
from urllib.parse import parse_qs

from .chat import (ChatAdapter, ChatError, Response, answer_text, decided_line, decision_text, empty_response,
                   http_json, in_background, json_response, status_line)

log = logging.getLogger("dispatchbox.slack")

REPLAY_WINDOW = 300
_MENTION = re.compile(r"<@[A-Z0-9]+>")


def esc(text: str) -> str:
    """Escape the three characters Slack mrkdwn treats as control characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def link_md(link: str, label: str) -> str:
    return f"<{link}|{label}>" if link.startswith(("http://", "https://")) else ""


def sign(secret: str, timestamp: str, body: bytes) -> str:
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


class SlackAdapter(ChatAdapter):
    name = "slack"

    def __init__(self, hub, bot_token: str, signing_secret: str, api_base: str = "https://slack.com/api",
                 clock=time.time):
        super().__init__(hub)
        self.bot_token = bot_token
        self.signing_secret = signing_secret
        self.api_base = api_base.rstrip("/")
        self.clock = clock

    # ------------------------------------------------------------------ Slack Web API

    def api(self, method: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = http_json("POST", f"{self.api_base}/{method}", body,
                         headers={"Authorization": f"Bearer {self.bot_token}"})
        if not resp.get("ok"):
            raise ChatError(f"slack {method} failed: {resp.get('error', resp)}")
        return resp

    def post(self, channel: str, text: str, blocks: list[dict[str, Any]] | None = None,
             thread_ts: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"channel": channel, "text": text}
        if blocks:
            body["blocks"] = blocks
        if thread_ts:
            body["thread_ts"] = thread_ts
        return self.api("chat.postMessage", body)

    # ------------------------------------------------------------------ incoming requests

    def verify(self, headers: dict[str, str], body: bytes) -> bool:
        ts = headers.get("x-slack-request-timestamp", "")
        sig = headers.get("x-slack-signature", "")
        if not ts.isdigit() or abs(self.clock() - int(ts)) > REPLAY_WINDOW:
            return False
        return hmac.compare_digest(sig, sign(self.signing_secret, ts, body))

    def handle(self, headers: dict[str, str], body: bytes) -> Response:
        if not self.verify(headers, body):
            return json_response({"error": "bad slack signature"}, 401)
        if headers.get("content-type", "").startswith("application/x-www-form-urlencoded"):
            form = parse_qs(body.decode())
            try:
                payload = json.loads(form.get("payload", ["{}"])[-1])
            except json.JSONDecodeError:
                return json_response({"error": "bad payload"}, 400)
            return self._interaction(payload)
        try:
            data = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return json_response({"error": "bad json"}, 400)
        if data.get("type") == "url_verification":
            return json_response({"challenge": data.get("challenge", "")})
        if data.get("type") == "event_callback":
            self._event(data)
        return empty_response()

    def _event(self, data: dict[str, Any]) -> None:
        ev = data.get("event") or {}
        kind = ev.get("type")
        if ev.get("bot_id") or ev.get("subtype") or not ev.get("user"):
            return  # bots (including ourselves), edits, deletes, joins
        if kind == "app_mention":
            etype = "message.mention"
        elif kind == "message" and ev.get("channel_type") == "im":
            etype = "message.dm"
        else:
            return
        user, channel = ev["user"], ev.get("channel", "")
        thread = ev.get("thread_ts") or ev.get("ts")
        if not self.user_allowed(user):
            log.warning("slack user %s is not in chat.slack.allowed_users", user)
            in_background(self._reply_safe, channel, thread,
                          f"You are not allowed to start dispatchbox runs. Ask the owner to add `{user}` to "
                          "chat.slack.allowed_users.")
            return
        text = _MENTION.sub("", ev.get("text", "")).strip()
        result = self.hub.ingest({
            "source": "slack", "type": etype, "subject": channel, "summary": text[:200],
            "payload": {"chat": channel, "thread": thread, "ts": ev.get("ts"), "user": user, "text": text},
            "dedupe_key": f"slack:{data.get('event_id') or ev.get('ts')}",
            "dedupe_window": 86400,
        })
        if self.settings.get("ack", True):
            msg = ack_text(result)
            if msg:
                in_background(self._reply_safe, channel, thread, msg)

    def _interaction(self, p: dict[str, Any]) -> Response:
        if p.get("type") != "block_actions":
            return empty_response()
        user = (p.get("user") or {}).get("id", "")
        channel = (p.get("container") or {}).get("channel_id") or (p.get("channel") or {}).get("id")
        ts = (p.get("container") or {}).get("message_ts") or (p.get("message") or {}).get("ts")
        for action in p.get("actions") or []:
            if not str(action.get("action_id", "")).startswith("dispatchbox_choice_"):
                continue
            try:
                value = json.loads(action.get("value") or "{}")
                run_id, choice = int(value["run"]), str(value["choice"])
            except (ValueError, KeyError, TypeError):
                continue
            ok, message, run = self.decide_from_chat(run_id, choice, user)
            if run and channel and ts:
                in_background(self._refresh_card, channel, ts, run)
            if not ok and p.get("response_url"):
                in_background(http_json, "POST", p["response_url"],
                              {"response_type": "ephemeral", "replace_original": False, "text": message})
        return empty_response()

    # ------------------------------------------------------------------ hub hooks

    def notify(self, run: dict[str, Any], link: str) -> None:
        if self.from_chat(run):
            return  # the originating conversation hears about it in run_finished
        channel = self.settings.get("notify_channel")
        if not channel:
            log.warning("slack: chat.slack.notify_channel is not set, skipping notification for run %s", run["id"])
            return
        if run["status"] == "awaiting_decision":
            self.post(channel, decision_fallback(run), decision_blocks(run, link))
        else:
            self.post(channel, result_text(run, link))

    def run_finished(self, run: dict[str, Any], link: str) -> None:
        origin = self.origin(run)
        if not origin or not self.settings.get("reply_on_finish", True):
            return
        if run["status"] == "awaiting_decision":
            self.post(origin["chat"], decision_fallback(run), decision_blocks(run, link), thread_ts=origin.get("thread"))
        elif run["status"] in {"succeeded", "failed", "lost"}:
            self.post(origin["chat"], result_text(run, link), thread_ts=origin.get("thread"))

    # ------------------------------------------------------------------ helpers

    def _reply_safe(self, channel: str, thread: str | None, text: str) -> None:
        try:
            self.post(channel, text, thread_ts=thread)
        except ChatError as e:
            log.error("slack reply failed: %s", e)

    def _refresh_card(self, channel: str, ts: str, run: dict[str, Any]) -> None:
        link = self.hub.run_link(run["id"])
        if run["status"] == "awaiting_decision":
            blocks, text = decision_blocks(run, link), decision_fallback(run)
        else:
            blocks, text = decided_blocks(run, link), decided_line(run)
        self.api("chat.update", {"channel": channel, "ts": ts, "text": text, "blocks": blocks})


def ack_text(result: dict[str, Any]) -> str:
    status = result.get("status")
    if status == "routed" and result.get("runs"):
        return "Got it. Queued as run " + ", ".join(f"#{r}" for r in result["runs"]) + "."
    if status == "coalesced":
        return "Added to queued run " + ", ".join(f"#{r}" for r in result.get("coalesced_into", [])) + "."
    if status == "unmatched":
        return "No dispatchbox rule matches this message, so nothing was started."
    return ""


def decision_fallback(run: dict[str, Any]) -> str:
    question, _, _ = decision_text(run)
    return f"Run #{run['id']} ({run['rule']}) needs your decision: {question}"


def decision_blocks(run: dict[str, Any], link: str) -> list[dict[str, Any]]:
    question, context, options = decision_text(run)
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{esc(question)}*"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": esc(status_line(run))}]},
    ]
    if context:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": esc(context)[:2900]}})
    if options:
        blocks.append({"type": "actions", "block_id": f"dispatchbox_run_{run['id']}", "elements": [
            {"type": "button", "action_id": f"dispatchbox_choice_{i}", "text": {"type": "plain_text", "text": opt[:75]},
             "value": json.dumps({"run": run["id"], "choice": opt}, ensure_ascii=False)[:2000],
             **({"style": "primary"} if i == 0 else {})}
            for i, opt in enumerate(options)
        ]})
    else:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "No options were given. Answer on the dashboard."}]})
    md = link_md(link, "Open in dispatchbox")
    if md:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": md}]})
    return blocks


def decided_blocks(run: dict[str, Any], link: str) -> list[dict[str, Any]]:
    question, _, _ = decision_text(run)
    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"~{esc(question)}~\n*{esc(decided_line(run))}*"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": esc(status_line(run))}]},
    ]
    md = link_md(link, "Open in dispatchbox")
    if md:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": md}]})
    return blocks


def result_text(run: dict[str, Any], link: str) -> str:
    lines = [esc(status_line(run))]
    answer = answer_text(run, 3000)
    if answer:
        lines.append(esc(answer))
    md = link_md(link, "details")
    if md:
        lines.append(md)
    return "\n".join(lines)
