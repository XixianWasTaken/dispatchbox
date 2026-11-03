"""Slack and Feishu adapters against fake platform APIs. Formats mirror the official docs."""

import base64
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from dispatchbox import chat as chat_mod
from dispatchbox import feishu as feishu_mod
from dispatchbox import slack as slack_mod
from dispatchbox.chat import ChatError, adapters_from_env
from dispatchbox.feishu import FeishuAdapter, encryption_available
from dispatchbox.hub import Hub
from dispatchbox.server import serve
from dispatchbox.slack import SlackAdapter, sign
from tests.helpers import FakeClock, write_rules

TOKEN = "hub-token"


def sync(fn, *args):
    fn(*args)


class FakeAPI:
    """Records every call and answers like Slack / Feishu would on success."""

    def __init__(self):
        self.calls = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                ctype = self.headers.get("Content-Type", "")
                body = dict(urllib.parse.parse_qsl(raw.decode())) if "form" in ctype else json.loads(raw or b"{}")
                outer.calls.append((self.path, dict(self.headers), body))
                if self.path.startswith("/slack/"):
                    resp = {"ok": True, "ts": f"17000000{len(outer.calls)}.0001", "channel": body.get("channel")}
                elif self.path == "/feishu/token":
                    resp = {"code": 0, "access_token": "jwt-token", "expires_in": 7200, "token_type": "Bearer"}
                elif self.path.startswith("/feishu/"):
                    resp = {"code": 0, "msg": "success", "data": {"message_id": f"om_new{len(outer.calls)}"}}
                else:
                    resp = {}
                data = json.dumps(resp).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def find(self, prefix):
        return [c for c in self.calls if c[0].startswith(prefix)]

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


RULES = {
    "rules": [
        {"name": "chat-task", "when": {"source": ["slack", "feishu"]}, "prompt": "Do: {summary}"},
        {"name": "after", "when": {"source": "dispatchbox", "type": "decision.made"}, "prompt": "Chose {payload.choice}"},
        {"name": "cron-job", "when": {"source": "cron"}, "prompt": "nightly", "notify": "on_failure"},
    ],
    "chat": {
        "slack": {"notify_channel": "C_OPS", "allowed_users": ["U_OWNER", "U_DEV"], "approvers": ["U_OWNER"]},
        "feishu": {"notify_to": "oc_ops", "notify_to_type": "chat_id", "allowed_users": ["ou_owner"]},
    },
}


class ChatBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api = FakeAPI()
        self.clock = FakeClock(time.time())
        self.hub = Hub(write_rules(self.tmp.name, RULES), str(Path(self.tmp.name, "b.db")), clock=self.clock,
                       public_url="https://dispatchbox.example.com")
        self.hub.notifier = lambda run, link: None
        self.hub.background = sync
        self.patches = [mock.patch.object(slack_mod, "in_background", sync),
                        mock.patch.object(feishu_mod, "in_background", sync)]
        for p in self.patches:
            p.start()
        self.srv = serve(self.hub, "127.0.0.1", 0, TOKEN)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.srv.shutdown()
        self.srv.server_close()
        self.hub.close()
        self.api.close()
        self.tmp.cleanup()

    def post(self, path, body: bytes, headers):
        req = urllib.request.Request(self.base + path, data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            with e:
                return e.code, None

    def ask_decision(self, run_id, options=("Delete", "Keep")):
        task = self.hub.claim("w")
        self.assertEqual(task["run_id"], run_id)
        out = "DISPATCHBOX_DECISION: " + json.dumps({"question": "Delete old rows?", "options": list(options),
                                               "context": "3 rows are stale"})
        self.hub.finish(run_id, "w", {"exit_code": 0, "output": out})


class SlackTest(ChatBase):
    SECRET = "slack-signing-secret"

    def setUp(self):
        super().setUp()
        self.slack = SlackAdapter(self.hub, "xoxb-test", self.SECRET, api_base=self.api.base + "/slack",
                                  clock=time.time)
        self.hub.adapters["slack"] = self.slack

    def send(self, body: bytes, ctype="application/json", ts=None, secret=None):
        ts = str(int(time.time()) if ts is None else ts)
        return self.post("/hooks/slack", body, {
            "Content-Type": ctype, "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": sign(secret or self.SECRET, ts, body)})

    def event(self, event, event_id="Ev1"):
        return self.send(json.dumps({"type": "event_callback", "event_id": event_id, "event": event}).encode())

    def click(self, run_id, choice, user="U_OWNER"):
        payload = {"type": "block_actions", "user": {"id": user},
                   "container": {"type": "message", "message_ts": "111.222", "channel_id": "C_DEV"},
                   "response_url": self.api.base + "/respond",
                   "actions": [{"action_id": "dispatchbox_choice_0", "type": "button",
                                "value": json.dumps({"run": run_id, "choice": choice})}]}
        body = urllib.parse.urlencode({"payload": json.dumps(payload)}).encode()
        return self.send(body, ctype="application/x-www-form-urlencoded")

    def test_sign_matches_official_example(self):
        # Example from docs.slack.dev "Verifying requests from Slack".
        body = (b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&team_domain=testteamnow&channel_id=G8PSS9T3V"
                b"&channel_name=foobar&user_id=U2CERLKJA&user_name=roadrunner&command=%2Fwebhook-collect&text="
                b"&response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands%2FT1DC2JH3J%2F397700885554%2F96rGlfmibIGlgcZRskXaIFfN"
                b"&trigger_id=398738663015.47445629121.803a0bc887a14d10d2c447fce8b6703c")
        self.assertEqual(sign("8f742231b10e8888abcd99yyyzzz85a5", "1531420618", body),
                         "v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503")

    def test_signature_and_url_verification(self):
        body = json.dumps({"type": "url_verification", "challenge": "abc", "token": "x"}).encode()
        self.assertEqual(self.send(body), (200, {"challenge": "abc"}))
        self.assertEqual(self.send(body, secret="wrong")[0], 401)
        self.assertEqual(self.send(body, ts=int(time.time()) - 301)[0], 401, "replay window is 5 minutes")

    def test_mention_starts_run_and_acks_in_thread(self):
        s, _ = self.event({"type": "app_mention", "user": "U_DEV", "text": "<@UBOT> fix the login bug",
                           "channel": "C_DEV", "ts": "100.1"})
        self.assertEqual(s, 200)
        run = self.hub.list_runs()[0]
        self.assertEqual(run["rule"], "chat-task")
        ev = self.hub.get_run(run["id"])["events"][0]
        self.assertEqual((ev["type"], ev["summary"]), ("message.mention", "fix the login bug"))
        (path, headers, body), = self.api.find("/slack/chat.postMessage")
        self.assertEqual(headers["Authorization"], "Bearer xoxb-test")
        self.assertEqual((body["channel"], body["thread_ts"]), ("C_DEV", "100.1"))
        self.assertIn(f"#{run['id']}", body["text"])

    def test_retried_event_is_deduped_even_after_ten_minutes(self):
        ev = {"type": "message", "channel_type": "im", "user": "U_DEV", "text": "hi", "channel": "D1", "ts": "1.1"}
        self.event(ev, "EvSame")
        self.clock.advance(3600)
        self.event(ev, "EvSame")
        self.assertEqual(len(self.hub.list_runs()), 1)
        self.assertEqual(len(self.api.find("/slack/chat.postMessage")), 1, "no second ack")

    def test_ignores_bots_and_rejects_unknown_users(self):
        self.event({"type": "app_mention", "user": "U_DEV", "bot_id": "B1", "text": "x", "channel": "C", "ts": "1"}, "E1")
        self.event({"type": "message", "subtype": "message_changed", "channel_type": "im", "channel": "D", "ts": "2"}, "E2")
        self.assertEqual(self.hub.list_runs(), [])
        self.event({"type": "app_mention", "user": "U_STRANGER", "text": "<@UBOT> rm -rf", "channel": "C", "ts": "3"}, "E3")
        self.assertEqual(self.hub.list_runs(), [])
        (_, _, body), = self.api.find("/slack/chat.postMessage")
        self.assertIn("U_STRANGER", body["text"])

    def test_allowlist_edit_applies_to_the_next_message(self):
        rules = json.loads(json.dumps(RULES))
        rules["chat"]["slack"]["allowed_users"].append("U_NEW")
        self.hub.rules_path.write_text(json.dumps(rules))
        os.utime(self.hub.rules_path, (time.time() + 5, time.time() + 5))
        self.event({"type": "app_mention", "user": "U_NEW", "text": "<@UBOT> hi", "channel": "C", "ts": "9"}, "E9")
        self.assertEqual(len(self.hub.list_runs()), 1)

    def test_decision_round_trip_in_thread(self):
        self.event({"type": "app_mention", "user": "U_DEV", "text": "<@UBOT> clean db", "channel": "C_DEV", "ts": "100.1"})
        run_id = self.hub.list_runs()[0]["id"]
        self.ask_decision(run_id)
        card = self.api.find("/slack/chat.postMessage")[-1][2]
        self.assertEqual(card["thread_ts"], "100.1", "decision goes back to the asking thread")
        actions = [b for b in card["blocks"] if b["type"] == "actions"][0]
        self.assertEqual([e["text"]["text"] for e in actions["elements"]], ["Delete", "Keep"])
        self.assertEqual(json.loads(actions["elements"][1]["value"]), {"run": run_id, "choice": "Keep"})
        self.assertNotIn("C_OPS", [c[2]["channel"] for c in self.api.find("/slack/chat.postMessage")],
                         "no duplicate card in the notify channel")

        # a developer who may start runs but is not an approver cannot decide
        self.click(run_id, "Keep", user="U_DEV")
        self.assertEqual(self.hub.get_run(run_id)["status"], "awaiting_decision")
        self.assertIn("not allowed", self.api.find("/respond")[-1][2]["text"])

        self.click(run_id, "Keep")
        self.assertEqual(self.hub.get_run(run_id)["status"], "decided")
        upd = self.api.find("/slack/chat.update")[-1][2]
        self.assertEqual((upd["channel"], upd["ts"]), ("C_DEV", "111.222"))
        self.assertIn("Decision: Keep", json.dumps(upd["blocks"]))

        follow = self.hub.claim("w")
        self.hub.finish(follow["run_id"], "w", {"exit_code": 0,
                        "output": "保留了这 3 行。\nDISPATCHBOX_STATUS: succeeded\nDISPATCHBOX_SUMMARY: kept rows"})
        last = self.api.find("/slack/chat.postMessage")[-1][2]
        self.assertEqual(last["thread_ts"], "100.1", "follow-up result found its way back through the parent run")
        self.assertIn("保留了这 3 行。", last["text"], "chat shows the agent's own answer")
        self.assertNotIn("DISPATCHBOX_", last["text"])
        self.assertNotIn("kept rows", last["text"])

    def test_notify_channel_for_runs_not_from_chat(self):
        self.hub.ingest({"source": "cron", "type": "nightly"})
        task = self.hub.claim("w")
        self.hub.finish(task["run_id"], "w", {"exit_code": 1, "output": "boom"})
        (_, _, body), = self.api.find("/slack/chat.postMessage")
        self.assertEqual(body["channel"], "C_OPS")
        self.assertIn("failed", body["text"])
        self.assertIn("<https://dispatchbox.example.com/runs/", body["text"])


class FeishuTest(ChatBase):
    VTOKEN = "verify-token"

    def make(self, encrypt_key=""):
        self.feishu = FeishuAdapter(self.hub, "cli_app", "secret", self.VTOKEN, encrypt_key=encrypt_key,
                                    api_base=self.api.base + "/feishu", token_url=self.api.base + "/feishu/token")
        self.hub.adapters["feishu"] = self.feishu

    def setUp(self):
        super().setUp()
        self.make()

    def send(self, obj, headers=None):
        return self.post("/hooks/feishu", json.dumps(obj).encode(), {"Content-Type": "application/json", **(headers or {})})

    def message(self, text, user="ou_owner", chat_type="p2p", message_id="om_1", sender_type="user"):
        return {"schema": "2.0", "header": {"event_id": "e-" + message_id, "event_type": "im.message.receive_v1",
                                            "token": self.VTOKEN, "app_id": "cli_app"},
                "event": {"sender": {"sender_id": {"open_id": user}, "sender_type": sender_type},
                          "message": {"message_id": message_id, "chat_id": "oc_dm", "chat_type": chat_type,
                                      "message_type": "text", "content": json.dumps({"text": text})}}}

    def card_click(self, run_id, choice, user="ou_owner"):
        return self.send({"schema": "2.0", "header": {"event_type": "card.action.trigger", "token": self.VTOKEN},
                          "event": {"operator": {"open_id": user}, "token": "c-1",
                                    "action": {"tag": "button", "value": {"run": run_id, "choice": choice}},
                                    "context": {"open_message_id": "om_card", "open_chat_id": "oc_dm"}}})

    def test_url_verification_and_token_check(self):
        self.assertEqual(self.send({"type": "url_verification", "challenge": "c1", "token": self.VTOKEN}),
                         (200, {"challenge": "c1"}))
        self.assertEqual(self.send({"type": "url_verification", "challenge": "c1", "token": "nope"})[0], 401)
        bad = self.message("hi")
        bad["header"]["token"] = "nope"
        self.assertEqual(self.send(bad)[0], 401)
        self.assertEqual(self.hub.list_runs(), [])

    def test_message_starts_run_and_replies(self):
        self.assertEqual(self.send(self.message("@_user_1 修一下登录", chat_type="group"))[0], 200)
        run = self.hub.get_run(self.hub.list_runs()[0]["id"])
        self.assertEqual((run["events"][0]["type"], run["events"][0]["summary"]), ("message.mention", "修一下登录"))
        token_calls = self.api.find("/feishu/token")
        self.assertEqual(token_calls[0][2]["grant_type"], "client_credentials")
        path, headers, body = self.api.find("/feishu/im/v1/messages/om_1/reply")[0]
        self.assertEqual(headers["Authorization"], "Bearer jwt-token")
        self.assertEqual(body["msg_type"], "text")
        self.assertIn(f"#{run['id']}", json.loads(body["content"])["text"])

    def test_ignores_bots_and_strangers_and_dedupes(self):
        self.send(self.message("hi", sender_type="bot", message_id="om_b"))
        self.send(self.message("hi", user="ou_stranger", message_id="om_s"))
        self.assertEqual(self.hub.list_runs(), [])
        self.send(self.message("hi", message_id="om_x"))
        self.clock.advance(3 * 3600)  # Feishu may redeliver hours later
        self.send(self.message("hi", message_id="om_x"))
        self.assertEqual(len(self.hub.list_runs()), 1)
        self.assertEqual(len(self.api.find("/feishu/token")), 1, "token is cached")

    def test_decision_card_and_click(self):
        self.send(self.message("清理数据库"))
        run_id = self.hub.list_runs()[0]["id"]
        self.ask_decision(run_id)
        path, _, body = self.api.find("/feishu/im/v1/messages/om_1/reply")[-1]
        self.assertEqual(body["msg_type"], "interactive")
        card = json.loads(body["content"])
        self.assertEqual((card["schema"], card["config"]["update_multi"]), ("2.0", True))
        buttons = [e for e in card["body"]["elements"] if e["tag"] == "button"]
        self.assertEqual([b["text"]["content"] for b in buttons], ["Delete", "Keep"])
        self.assertEqual(buttons[0]["behaviors"], [{"type": "callback", "value": {"run": run_id, "choice": "Delete"}}])

        status, resp = self.card_click(run_id, "Delete", user="ou_stranger")
        self.assertEqual(resp["toast"]["type"], "error")
        self.assertEqual(self.hub.get_run(run_id)["status"], "awaiting_decision")

        status, resp = self.card_click(run_id, "Delete")
        self.assertEqual((status, resp["toast"]["type"]), (200, "success"))
        self.assertEqual(resp["card"]["type"], "raw")
        self.assertEqual(resp["card"]["data"]["schema"], "2.0", "a 2.0 card must stay 2.0 after an update")
        self.assertIn("Decision: Delete", json.dumps(resp["card"]["data"], ensure_ascii=False))
        self.assertEqual(self.hub.get_run(run_id)["status"], "decided")

        status, resp = self.card_click(run_id, "Keep")
        self.assertEqual(resp["toast"]["type"], "error", "second click is refused")

    def test_other_platform_stays_quiet_for_a_feishu_run(self):
        self.hub.adapters["slack"] = SlackAdapter(self.hub, "xoxb-test", "s", api_base=self.api.base + "/slack")
        self.send(self.message("清理数据库"))
        run_id = self.hub.list_runs()[0]["id"]
        self.ask_decision(run_id)
        self.assertEqual(len(self.api.find("/feishu/im/v1/messages/om_1/reply")), 2, "ack and card in feishu")
        self.assertEqual(self.api.find("/slack/chat.postMessage"), [],
                         "no second card in the slack notify channel, which has C_OPS configured")
        self.card_click(run_id, "Keep")
        follow = self.hub.claim("w")
        self.hub.finish(follow["run_id"], "w", {"exit_code": 0, "output": "kept\nDISPATCHBOX_STATUS: succeeded"})
        self.assertEqual(self.api.find("/slack/chat.postMessage"), [])

    def test_notify_target_for_runs_not_from_chat(self):
        self.hub.ingest({"source": "cron", "type": "nightly"})
        task = self.hub.claim("w")
        self.hub.finish(task["run_id"], "w", {"exit_code": 1, "output": "boom"})
        path, _, body = self.api.find("/feishu/im/v1/messages?")[0]
        self.assertIn("receive_id_type=chat_id", path)
        self.assertEqual(body["receive_id"], "oc_ops")

    @unittest.skipUnless(encryption_available(), "needs the cryptography package")
    def test_decrypt_matches_official_example(self):
        # Example published in Feishu's event and callback docs.
        self.assertEqual(feishu_mod.decrypt("test key", "P37w+VZImNgPEO1RBhJ6RtKl7n6zymIbEG1pReEzghk="), b"hello world")

    @unittest.skipUnless(encryption_available(), "needs the cryptography package")
    def test_encrypted_payload_and_signature(self):
        from cryptography.hazmat.primitives import padding
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        key = "my-encrypt-key"
        self.make(encrypt_key=key)

        def encrypt(obj):
            iv = os.urandom(16)
            p = padding.PKCS7(128).padder()
            data = p.update(json.dumps(obj).encode()) + p.finalize()
            enc = Cipher(algorithms.AES(hashlib.sha256(key.encode()).digest()), modes.CBC(iv)).encryptor()
            return {"encrypt": base64.b64encode(iv + enc.update(data) + enc.finalize()).decode()}

        def signed(obj, sig_key=key):
            body = json.dumps(obj).encode()
            ts, nonce = "1700000000", "n1"
            sig = hashlib.sha256((ts + nonce + sig_key).encode() + body).hexdigest()
            return self.post("/hooks/feishu", body, {"Content-Type": "application/json", "X-Lark-Request-Timestamp": ts,
                                                     "X-Lark-Request-Nonce": nonce, "X-Lark-Signature": sig})

        challenge = encrypt({"type": "url_verification", "challenge": "enc-c", "token": self.VTOKEN})
        self.assertEqual(self.send(challenge), (200, {"challenge": "enc-c"}), "challenge is not signed")
        self.assertEqual(signed(encrypt(self.message("hi")), sig_key="wrong")[0], 401)
        self.assertEqual(self.send(self.message("hi", message_id="om_plain"))[0], 401, "plain payload refused")
        self.assertEqual(signed(encrypt(self.message("hi")))[0], 200)
        self.assertEqual(len(self.hub.list_runs()), 1)


class EnvTest(unittest.TestCase):
    def test_half_configured_is_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            hub = Hub(write_rules(d, RULES), str(Path(d, "b.db")))
            try:
                self.assertEqual(adapters_from_env(hub, {}), {})
                with self.assertRaises(ChatError):
                    adapters_from_env(hub, {"DISPATCHBOX_SLACK_BOT_TOKEN": "x"})
                with self.assertRaises(ChatError):
                    adapters_from_env(hub, {"DISPATCHBOX_FEISHU_APP_ID": "a", "DISPATCHBOX_FEISHU_APP_SECRET": "b"})
                both = adapters_from_env(hub, {"DISPATCHBOX_SLACK_BOT_TOKEN": "x", "DISPATCHBOX_SLACK_SIGNING_SECRET": "y",
                                               "DISPATCHBOX_FEISHU_APP_ID": "a", "DISPATCHBOX_FEISHU_APP_SECRET": "b",
                                               "DISPATCHBOX_FEISHU_VERIFICATION_TOKEN": "c"})
                self.assertEqual(sorted(both), ["feishu", "slack"])
            finally:
                hub.close()

    def test_answer_text(self):
        from dispatchbox.chat import answer_text
        run = {"output_tail": "line one\nline two\nDISPATCHBOX_STATUS: succeeded\nDISPATCHBOX_SUMMARY: short", "summary": "short"}
        self.assertEqual(answer_text(run, 100), "line one\nline two")
        self.assertEqual(answer_text({"output_tail": "DISPATCHBOX_SUMMARY: s\n", "summary": "s"}, 100), "s",
                         "falls back to the summary when the agent said nothing else")
        self.assertEqual(answer_text({"output_tail": "x" * 50}, 10), "x" * 9 + "…")
        self.assertEqual(answer_text({}, 10), "")

    def test_chat_config_validation(self):
        from dispatchbox.rules import load_config
        with self.assertRaises(ValueError):
            load_config({"chat": {"teams": {}}})
        with self.assertRaises(ValueError):
            load_config({"chat": {"slack": {"allowed_users": "U1"}}})


if __name__ == "__main__":
    unittest.main()
