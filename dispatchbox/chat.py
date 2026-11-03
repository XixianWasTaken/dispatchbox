"""Shared plumbing for chat platforms (Feishu, Slack).

An adapter does four things:
  1. turns chat messages that mention the bot (or DM it) into dispatchbox events,
  2. posts a decision card with one button per option when a run needs a human,
  3. records the button click as a decision and updates the card in place,
  4. replies in the original thread when a run that came from chat finishes.

Platform specifics (auth, signatures, payload shapes, card formats) live in feishu.py and slack.py.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any, Callable

from .hub import DECISION_MARK, STATUS_MARK, SUMMARY_MARK
from .net import ssl_context

if TYPE_CHECKING:
    from .hub import Hub

log = logging.getLogger("dispatchbox.chat")

STATUS_LABEL = {
    "succeeded": "✅ succeeded",
    "failed": "❌ failed",
    "lost": "⚠️ lost",
    "awaiting_decision": "🟠 needs your decision",
    "decided": "☑️ decided",
}

# (status, headers, body) returned to the HTTP layer
Response = tuple[int, dict[str, str], bytes]


def json_response(obj: Any, status: int = 200) -> Response:
    return status, {"Content-Type": "application/json"}, json.dumps(obj, ensure_ascii=False).encode()


def empty_response(status: int = 200) -> Response:
    return status, {}, b""


class ChatError(Exception):
    pass


def http_json(method: str, url: str, body: Any = None, headers: dict[str, str] | None = None,
              timeout: float = 10) -> dict[str, Any]:
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json; charset=utf-8", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise ChatError(f"{method} {url} -> HTTP {e.code}: {raw[:300]!r}") from e
    return json.loads(raw) if raw else {}


def in_background(fn: Callable[..., Any], *args: Any) -> None:
    """Chat platforms want an answer within ~3 seconds, so slow work (API calls) runs on a thread."""
    def run() -> None:
        try:
            fn(*args)
        except Exception:
            log.exception("background chat task failed")
    threading.Thread(target=run, daemon=True).start()


class ChatAdapter:
    name = "chat"          # also the event source name and the URL suffix: /hooks/<name>

    def __init__(self, hub: "Hub"):
        self.hub = hub

    # ---- configuration from the "chat" section of the rules file, re-read on every use so edits apply live

    @property
    def settings(self) -> dict[str, Any]:
        # Chat requests are checked before ingest, so pick up allowlist edits here instead of
        # waiting for the once-a-minute cron tick.
        self.hub.reload_if_changed()
        return (self.hub.config.chat or {}).get(self.name) or {}

    def user_allowed(self, user_id: str, key: str = "allowed_users") -> bool:
        """Nobody is allowed unless listed. "*" allows everyone in the workspace."""
        allowed = self.settings.get(key)
        if allowed is None and key == "approvers":
            allowed = self.settings.get("allowed_users")
        allowed = allowed or []
        return "*" in allowed or user_id in allowed

    # ---- hooks called by the hub

    def notify(self, run: dict[str, Any], link: str) -> None:
        raise NotImplementedError

    def run_finished(self, run: dict[str, Any], link: str) -> None:
        """Reply in the originating thread. Called for every run that reaches a resting state."""
        raise NotImplementedError

    def handle(self, headers: dict[str, str], body: bytes) -> Response:
        """Handle a request to /hooks/<name>."""
        raise NotImplementedError

    # ---- helpers shared by adapters

    def origin(self, run: dict[str, Any]) -> dict[str, Any] | None:
        """The chat message this run (or the run it continues) came from, if it came from this platform."""
        found = self._chat_origin(run, {self.name})
        return found[1] if found else None

    def from_chat(self, run: dict[str, Any]) -> bool:
        """True if the run came from any connected chat platform. That platform answers in the
        original conversation, so the others stay quiet instead of offering a second set of buttons."""
        return self._chat_origin(run, set(self.hub.adapters) | {self.name}) is not None

    def _chat_origin(self, run: dict[str, Any], sources: set[str]) -> tuple[str, dict[str, Any]] | None:
        seen: set[int] = set()
        cur: dict[str, Any] | None = run
        while cur and cur["id"] not in seen:
            seen.add(cur["id"])
            for e in cur.get("events") or []:
                if e["source"] in sources and isinstance(e.get("payload"), dict) and e["payload"].get("chat"):
                    return e["source"], e["payload"]
            parent = cur.get("parent_run") or cur.get("retry_of")
            cur = self.hub.get_run(parent) if parent else None
        return None

    def decide_from_chat(self, run_id: int, choice: str, user_id: str) -> tuple[bool, str, dict[str, Any] | None]:
        """Record a button click. Returns (ok, message for the clicker, run after the attempt)."""
        from .hub import HubError
        if not self.user_allowed(user_id, "approvers"):
            log.warning("%s user %s tried to decide run %s but is not an approver", self.name, user_id, run_id)
            return False, "You are not allowed to decide dispatchbox runs.", self.hub.get_run(run_id)
        try:
            self.hub.decide(run_id, choice, by=f"{self.name}:{user_id}")
        except HubError as e:
            return False, str(e), self.hub.get_run(run_id)
        return True, f"Recorded: {choice}", self.hub.get_run(run_id)


def decision_text(run: dict[str, Any]) -> tuple[str, str, list[str]]:
    """(question, context, options) for a run awaiting a decision."""
    req = run.get("decision_request") or {}
    return str(req.get("question") or "The agent needs a decision."), str(req.get("context") or ""), \
        [str(o) for o in (req.get("options") or [])][:5]


def answer_text(run: dict[str, Any], limit: int) -> str:
    """What the agent actually said, minus the DISPATCHBOX_* contract lines; the summary only if that is empty.
    The answer is in the asker's language and has the detail; the summary is a one-liner for lists."""
    lines = [l for l in (run.get("output_tail") or "").splitlines()
             if not l.strip().startswith((DECISION_MARK, STATUS_MARK, SUMMARY_MARK))]
    text = "\n".join(lines).strip() or str(run.get("summary") or "")
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def status_line(run: dict[str, Any]) -> str:
    return f"{STATUS_LABEL.get(run['status'], run['status'])} · run #{run['id']} · {run['rule']}"


def decided_line(run: dict[str, Any]) -> str:
    d = run.get("decision") or {}
    who = str(d.get("by", "")).split(":", 1)[-1]
    return f"Decision: {d.get('choice', '')}" + (f" (by {who})" if who else "")


def adapters_from_env(hub: "Hub", env: dict[str, str]) -> dict[str, ChatAdapter]:
    """Build the adapters whose credentials are present. A half-configured platform is an error, not a skip."""
    adapters: dict[str, ChatAdapter] = {}

    slack_keys = ("DISPATCHBOX_SLACK_BOT_TOKEN", "DISPATCHBOX_SLACK_SIGNING_SECRET")
    have = [k for k in slack_keys if env.get(k)]
    if have and len(have) != len(slack_keys):
        missing = ", ".join(k for k in slack_keys if not env.get(k))
        raise ChatError(f"Slack is half configured: set {missing} as well, or unset {', '.join(have)}")
    if have:
        from .slack import SlackAdapter
        adapters["slack"] = SlackAdapter(hub, env["DISPATCHBOX_SLACK_BOT_TOKEN"], env["DISPATCHBOX_SLACK_SIGNING_SECRET"],
                                         api_base=env.get("DISPATCHBOX_SLACK_API", "https://slack.com/api"))

    feishu_keys = ("DISPATCHBOX_FEISHU_APP_ID", "DISPATCHBOX_FEISHU_APP_SECRET", "DISPATCHBOX_FEISHU_VERIFICATION_TOKEN")
    have = [k for k in feishu_keys if env.get(k)]
    if have and len(have) != len(feishu_keys):
        missing = ", ".join(k for k in feishu_keys if not env.get(k))
        raise ChatError(f"Feishu is half configured: set {missing} as well, or unset {', '.join(have)}")
    if have:
        from .feishu import API_BASE, TOKEN_URL, FeishuAdapter
        adapters["feishu"] = FeishuAdapter(
            hub, env["DISPATCHBOX_FEISHU_APP_ID"], env["DISPATCHBOX_FEISHU_APP_SECRET"], env["DISPATCHBOX_FEISHU_VERIFICATION_TOKEN"],
            encrypt_key=env.get("DISPATCHBOX_FEISHU_ENCRYPT_KEY", ""),
            api_base=env.get("DISPATCHBOX_FEISHU_API", API_BASE), token_url=env.get("DISPATCHBOX_FEISHU_TOKEN_URL", TOKEN_URL))

    for name in adapters:
        if not (hub.config.chat or {}).get(name):
            log.warning("%s credentials are set but the rules file has no chat.%s section, so nobody is allowed "
                        "to use it yet", name, name)
    return adapters
