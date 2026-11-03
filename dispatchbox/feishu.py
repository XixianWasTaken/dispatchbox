"""Feishu (飞书) adapter for a self-built app, standard library only unless an Encrypt Key is used.

Formats follow open.feishu.cn docs: OAuth client_credentials token, im/v1 messages and replies,
card JSON 2.0 with callback buttons, card.action.trigger callbacks, im.message.receive_v1 events,
url_verification challenges, and AES-256-CBC payload encryption when an Encrypt Key is configured.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .chat import (ChatAdapter, ChatError, Response, answer_text, decided_line, decision_text, http_json,
                   in_background, json_response, status_line)
from .net import ssl_context

log = logging.getLogger("dispatchbox.feishu")

_AT_PLACEHOLDER = re.compile(r"@_user_\d+")
TOKEN_URL = "https://accounts.feishu.cn/oauth/v3/token"
API_BASE = "https://open.feishu.cn/open-apis"


def decrypt(encrypt_key: str, blob: str) -> bytes:
    """AES-256-CBC, key = sha256(encrypt_key), first 16 bytes of the base64-decoded blob are the IV, PKCS7."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError as e:  # checked at startup too, this is a second line of defence
        raise ChatError("Feishu Encrypt Key is set but the 'cryptography' package is not installed") from e
    raw = base64.b64decode(blob)
    key = hashlib.sha256(encrypt_key.encode()).digest()
    dec = Cipher(algorithms.AES(key), modes.CBC(raw[:16])).decryptor()
    padded = dec.update(raw[16:]) + dec.finalize()
    pad = padded[-1]
    if not 1 <= pad <= 16 or padded[-pad:] != bytes([pad]) * pad:
        raise ChatError("bad padding in Feishu payload")
    return padded[:-pad]


def encryption_available() -> bool:
    try:
        import cryptography.hazmat.primitives.ciphers  # noqa: F401
        return True
    except ImportError:
        return False


class FeishuAdapter(ChatAdapter):
    name = "feishu"

    def __init__(self, hub, app_id: str, app_secret: str, verification_token: str, encrypt_key: str = "",
                 api_base: str = API_BASE, token_url: str = TOKEN_URL):
        super().__init__(hub)
        if encrypt_key and not encryption_available():
            raise ChatError("DISPATCHBOX_FEISHU_ENCRYPT_KEY is set, which needs the 'cryptography' package. "
                            "Install it with `python3 -m pip install cryptography`, or clear the Encrypt Key "
                            "in the Feishu console and unset DISPATCHBOX_FEISHU_ENCRYPT_KEY.")
        self.app_id = app_id
        self.app_secret = app_secret
        self.verification_token = verification_token
        self.encrypt_key = encrypt_key
        self.api_base = api_base.rstrip("/")
        self.token_url = token_url
        self._token = ""
        self._token_expiry = 0.0
        self._token_lock = threading.Lock()

    # ------------------------------------------------------------------ Feishu Open API

    def token(self) -> str:
        with self._token_lock:
            if self._token and time.time() < self._token_expiry - 300:
                return self._token
            body = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self.app_id,
                                           "client_secret": self.app_secret}).encode()
            req = urllib.request.Request(self.token_url, data=body, method="POST",
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
            try:
                with urllib.request.urlopen(req, timeout=10, context=ssl_context()) as resp:
                    data = json.loads(resp.read())
            except urllib.error.HTTPError as e:
                data = json.loads(e.read() or b"{}")
            # The current endpoint returns access_token/expires_in; the historical one tenant_access_token/expire.
            tok = data.get("access_token") or data.get("tenant_access_token")
            if data.get("code", 0) != 0 or not tok:
                raise ChatError(f"feishu token request failed: {data.get('error_description') or data.get('msg') or data}")
            self._token = tok
            self._token_expiry = time.time() + int(data.get("expires_in") or data.get("expire") or 7200)
            return tok

    def api(self, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = http_json(method, self.api_base + path, body, headers={"Authorization": f"Bearer {self.token()}"})
        if resp.get("code", -1) != 0:
            raise ChatError(f"feishu {path} failed: code {resp.get('code')} {resp.get('msg')}")
        return resp.get("data") or {}

    def send(self, receive_id: str, id_type: str, msg_type: str, content: dict[str, Any]) -> str:
        data = self.api("POST", f"/im/v1/messages?receive_id_type={urllib.parse.quote(id_type)}",
                        {"receive_id": receive_id, "msg_type": msg_type,
                         "content": json.dumps(content, ensure_ascii=False)})
        return data.get("message_id", "")

    def reply(self, message_id: str, msg_type: str, content: dict[str, Any]) -> str:
        data = self.api("POST", f"/im/v1/messages/{urllib.parse.quote(message_id)}/reply",
                        {"msg_type": msg_type, "content": json.dumps(content, ensure_ascii=False)})
        return data.get("message_id", "")

    # ------------------------------------------------------------------ incoming requests

    def _open(self, headers: dict[str, str], body: bytes) -> dict[str, Any] | None:
        """Decrypt and authenticate a request. Returns the inner JSON, or None if it is not genuine."""
        try:
            data = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return None
        if "encrypt" in data:
            if not self.encrypt_key:
                log.error("feishu sent an encrypted payload but DISPATCHBOX_FEISHU_ENCRYPT_KEY is not set")
                return None
            try:
                inner = json.loads(decrypt(self.encrypt_key, data["encrypt"]))
            except (ChatError, ValueError) as e:
                log.error("cannot decrypt feishu payload: %s", e)
                return None
            if inner.get("type") != "url_verification":
                ts = headers.get("x-lark-request-timestamp", "")
                nonce = headers.get("x-lark-request-nonce", "")
                expected = hashlib.sha256((ts + nonce + self.encrypt_key).encode() + body).hexdigest()
                if not hmac.compare_digest(headers.get("x-lark-signature", ""), expected):
                    log.error("feishu signature mismatch")
                    return None
            data = inner
        elif self.encrypt_key:
            log.error("feishu sent a plain payload but an Encrypt Key is configured")
            return None
        token = data.get("token") if data.get("type") == "url_verification" else (data.get("header") or {}).get("token")
        if not hmac.compare_digest(str(token or ""), self.verification_token):
            log.error("feishu verification token mismatch")
            return None
        return data

    def handle(self, headers: dict[str, str], body: bytes) -> Response:
        data = self._open(headers, body)
        if data is None:
            return json_response({"error": "not a genuine feishu request"}, 401)
        if data.get("type") == "url_verification":
            return json_response({"challenge": data.get("challenge", "")})
        event_type = (data.get("header") or {}).get("event_type")
        if event_type == "card.action.trigger":
            return json_response(self._card_action(data.get("event") or {}))
        if event_type == "im.message.receive_v1":
            self._message(data.get("event") or {})
        return json_response({})

    def _message(self, ev: dict[str, Any]) -> None:
        sender = ev.get("sender") or {}
        msg = ev.get("message") or {}
        if sender.get("sender_type") != "user" or msg.get("message_type") != "text":
            return
        user = (sender.get("sender_id") or {}).get("open_id", "")
        message_id = msg.get("message_id", "")
        if not self.user_allowed(user):
            log.warning("feishu user %s is not in chat.feishu.allowed_users", user)
            in_background(self._reply_safe, message_id,
                          f"You are not allowed to start dispatchbox runs. Ask the owner to add {user} to "
                          "chat.feishu.allowed_users.")
            return
        try:
            text = json.loads(msg.get("content") or "{}").get("text", "")
        except json.JSONDecodeError:
            text = ""
        text = _AT_PLACEHOLDER.sub("", text).strip()
        chat = msg.get("chat_id", "")
        result = self.hub.ingest({
            "source": "feishu", "type": "message.dm" if msg.get("chat_type") == "p2p" else "message.mention",
            "subject": chat, "summary": text[:200],
            "payload": {"chat": chat, "message_id": message_id, "thread": msg.get("root_id") or message_id,
                        "user": user, "text": text},
            # Feishu's docs say to dedupe messages on message_id rather than event_id.
            "dedupe_key": f"feishu:{message_id}",
            "dedupe_window": 86400,
        })
        if self.settings.get("ack", True):
            from .slack import ack_text  # same wording on both platforms
            msg_text = ack_text(result)
            if msg_text:
                in_background(self._reply_safe, message_id, msg_text)

    def _card_action(self, ev: dict[str, Any]) -> dict[str, Any]:
        value = (ev.get("action") or {}).get("value")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                value = {}
        try:
            run_id, choice = int(value["run"]), str(value["choice"])
        except (KeyError, TypeError, ValueError):
            return {"toast": {"type": "error", "content": "Not a dispatchbox button"}}
        user = (ev.get("operator") or {}).get("open_id", "")
        ok, message, run = self.decide_from_chat(run_id, choice, user)
        resp: dict[str, Any] = {"toast": {"type": "success" if ok else "error", "content": message[:100]}}
        if run:
            link = self.hub.run_link(run_id)
            card = decision_card(run, link) if run["status"] == "awaiting_decision" else decided_card(run, link)
            resp["card"] = {"type": "raw", "data": card}
        return resp

    # ------------------------------------------------------------------ hub hooks

    def notify(self, run: dict[str, Any], link: str) -> None:
        if self.from_chat(run):
            return  # the originating conversation hears about it in run_finished
        target = self.settings.get("notify_to")
        if not target:
            log.warning("feishu: chat.feishu.notify_to is not set, skipping notification for run %s", run["id"])
            return
        id_type = self.settings.get("notify_to_type", "chat_id")
        if run["status"] == "awaiting_decision":
            self.send(target, id_type, "interactive", decision_card(run, link))
        else:
            self.send(target, id_type, "interactive", result_card(run, link))

    def run_finished(self, run: dict[str, Any], link: str) -> None:
        origin = self.origin(run)
        if not origin or not self.settings.get("reply_on_finish", True):
            return
        if run["status"] == "awaiting_decision":
            self.reply(origin["message_id"], "interactive", decision_card(run, link))
        elif run["status"] in {"succeeded", "failed", "lost"}:
            self.reply(origin["message_id"], "interactive", result_card(run, link))

    def _reply_safe(self, message_id: str, text: str) -> None:
        try:
            self.reply(message_id, "text", {"text": text})
        except ChatError as e:
            log.error("feishu reply failed: %s", e)


# ---------------------------------------------------------------------- cards (JSON 2.0)

_TEMPLATE = {"awaiting_decision": "orange", "succeeded": "green", "failed": "red", "lost": "red", "decided": "grey"}


def _card(title: str, template: str, elements: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": title[:100]}, "template": template},
        "body": {"elements": elements},
    }


def _link_md(link: str) -> list[dict[str, Any]]:
    if link.startswith(("http://", "https://")):
        return [{"tag": "markdown", "content": f"[Open in dispatchbox]({link})"}]
    return []


def decision_card(run: dict[str, Any], link: str) -> dict[str, Any]:
    question, context, options = decision_text(run)
    elements: list[dict[str, Any]] = [
        {"tag": "markdown", "content": f"**{question}**"},
        {"tag": "markdown", "content": status_line(run)},
    ]
    if context:
        elements.append({"tag": "markdown", "content": context[:2000]})
    for i, opt in enumerate(options):
        elements.append({
            "tag": "button", "text": {"tag": "plain_text", "content": opt[:60]},
            "type": "primary" if i == 0 else "default",
            "behaviors": [{"type": "callback", "value": {"run": run["id"], "choice": opt}}],
        })
    if not options:
        elements.append({"tag": "markdown", "content": "No options were given. Answer on the dashboard."})
    return _card(f"Needs your decision · run #{run['id']}", "orange", elements + _link_md(link))


def decided_card(run: dict[str, Any], link: str) -> dict[str, Any]:
    question, _, _ = decision_text(run)
    elements = [
        {"tag": "markdown", "content": f"~~{question}~~"},
        {"tag": "markdown", "content": f"**{decided_line(run)}**"},
        {"tag": "markdown", "content": status_line(run)},
    ]
    return _card(f"Decided · run #{run['id']}", "grey", elements + _link_md(link))


def result_card(run: dict[str, Any], link: str) -> dict[str, Any]:
    elements = [{"tag": "markdown", "content": status_line(run)}]
    answer = answer_text(run, 2000)
    if answer:
        elements.append({"tag": "markdown", "content": answer})
    return _card(f"Run #{run['id']} {run['status']}", _TEMPLATE.get(run["status"], "blue"), elements + _link_md(link))
