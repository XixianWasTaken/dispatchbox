"""End-to-end: real HTTP hub, real worker threads, fake agent binaries."""

import hashlib
import hmac
import json
import os
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from dispatchbox.hub import Hub
from dispatchbox.server import serve
from dispatchbox.worker import WorkerConfig, resolve_cwd, run_task, serve_worker, HubClient
from tests.helpers import write_rules

TOKEN = "test-token-123"

FAKE_CLAUDE = r"""#!/usr/bin/env python3
import json, os, sys
prompt = sys.stdin.read()
args = sys.argv[1:]
open(os.path.join(os.getcwd(), "seen_prompt.txt"), "w").write(prompt)
if "--resume" in args:
    text = "continued after decision\nDISPATCHBOX_SUMMARY: resumed " + args[args.index("--resume") + 1]
elif "IMPOSSIBLE" in prompt:
    text = "could not do it\nDISPATCHBOX_STATUS: failed\nDISPATCHBOX_SUMMARY: blocked by permissions"
elif "DELETE" in prompt:
    text = 'DISPATCHBOX_DECISION: {"question": "Delete the old rows?", "options": ["delete", "keep"]}'
else:
    text = "did the work\nDISPATCHBOX_SUMMARY: reviewed " + os.environ["DISPATCHBOX_RULE"]
assert "DISPATCHBOX_TOKEN" not in os.environ, "token leaked to agent"
cost = 0.03 if "--resume" in args else 0.01   # like the real CLI, a resumed session reports its running total
print(json.dumps({"type": "result", "result": text, "session_id": "sess-42", "is_error": False,
                  "total_cost_usd": cost, "usage": {"input_tokens": 10, "cache_read_input_tokens": 90,
                                                    "output_tokens": 7}}))
"""


def http(method, url, body=None, token=TOKEN, headers=None, form=None):
    h = {"Accept": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    if form is not None:
        data = form.encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    h.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=h)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=20) as r:
            raw = r.read()
            return r.status, raw.decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


class E2ETest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.repo.mkdir()
        (root / "outside").mkdir()
        self.fake = root / "fake-claude"
        self.fake.write_text(FAKE_CLAUDE)
        self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC)
        rules = {
            "require_decision_for": ["delete_data"],
            "rules": [
                {"name": "review", "when": {"source": "github", "types": ["pr.*"]}, "runner": "claude",
                 "cwd": str(self.repo), "prompt": "Review {subject}"},
                {"name": "cleanup", "when": {"source": "ops", "type": "cleanup"}, "runner": "claude",
                 "cwd": str(self.repo), "prompt": "DELETE stale rows"},
                {"name": "followup", "when": {"source": "dispatchbox", "type": "decision.made"}, "runner": "claude",
                 "cwd": str(self.repo), "prompt": "Owner chose {payload.choice}"},
                {"name": "escape", "when": {"source": "ops", "type": "escape"}, "runner": "claude",
                 "cwd": str(root / "outside"), "prompt": "x"},
                {"name": "impossible", "when": {"source": "ops", "type": "impossible"}, "runner": "claude",
                 "cwd": str(self.repo), "prompt": "IMPOSSIBLE task"},
                {"name": "slow", "when": {"source": "ops", "type": "slow"}, "runner": "shell",
                 "cwd": str(self.repo), "command": "sleep 30", "timeout": "1s"},
            ],
        }
        self.hub = Hub(write_rules(self.tmp.name, rules), str(root / "b.db"))
        self.hub.notifier = lambda run, link: None
        self.srv = serve(self.hub, "127.0.0.1", 0, TOKEN, github_secret="gh-secret")
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.cfg = WorkerConfig(hub=self.base, name="m4", token=TOKEN, wait=2, allow_dirs=[str(self.repo)],
                                runners=["claude", "shell"], claude_bin=str(self.fake))
        self.stop = threading.Event()

    def tearDown(self):
        self.stop.set()
        self.srv.shutdown()
        self.srv.server_close()
        self.hub.close()
        self.tmp.cleanup()

    def start_worker(self):
        threading.Thread(target=serve_worker, args=(self.cfg, self.stop), daemon=True).start()

    def wait_status(self, run_id, statuses, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            run = self.hub.get_run(run_id)
            if run["status"] in statuses:
                return run
            time.sleep(0.1)
        self.fail(f"run {run_id} stuck in {self.hub.get_run(run_id)['status']}")

    def test_auth(self):
        self.assertEqual(http("POST", self.base + "/events", {"source": "a", "type": "b"}, token=None)[0], 401)
        self.assertEqual(http("POST", self.base + "/events", {"source": "a", "type": "b"}, token="wrong")[0], 401)
        self.assertEqual(http("GET", self.base + "/claim?worker=x", token=None)[0], 401)
        self.assertEqual(http("GET", self.base + "/api/runs", token=None)[0], 401)
        self.assertEqual(http("GET", self.base + "/healthz", token=None)[0], 200)
        self.assertEqual(http("POST", self.base + "/events", {"source": "a"})[0], 400)

    def test_api_errors_are_json_without_an_accept_header(self):
        no_accept = {"Accept": ""}
        s, body = http("POST", self.base + "/runs/999/decision", {"choice": "x"}, headers=no_accept)
        self.assertEqual(s, 404)
        self.assertEqual(json.loads(body), {"error": "run 999 not found"})
        s, body = http("POST", self.base + "/runs/999/decision", {"choice": "x"}, token=None, headers=no_accept)
        self.assertEqual(s, 401)
        self.assertIn("error", json.loads(body))
        # A browser posting the dashboard form still gets a page.
        s, body = http("POST", self.base + "/runs/999/decision", token=None, form="choice=x",
                       headers={"Accept": "text/html"})
        self.assertTrue(body.lstrip().startswith("<!doctype html>"), body[:80])

    def test_full_loop_with_decision_and_resume(self):
        self.start_worker()
        s, body = http("POST", self.base + "/events", {"source": "github", "type": "pr.opened", "subject": "r#1"})
        self.assertEqual(s, 200)
        run = self.wait_status(json.loads(body)["runs"][0], {"succeeded", "failed"})
        self.assertEqual(run["status"], "succeeded", run["output_tail"])
        self.assertEqual(run["summary"], "reviewed review")
        self.assertEqual(run["session_id"], "sess-42")
        self.assertEqual((run["cost_usd"], run["input_tokens"], run["output_tokens"]), (0.01, 100, 7))
        self.assertIn("Review r#1", (self.repo / "seen_prompt.txt").read_text())

        s, body = http("POST", self.base + "/events", {"source": "ops", "type": "cleanup", "subject": "db"})
        rid = json.loads(body)["runs"][0]
        run = self.wait_status(rid, {"awaiting_decision", "succeeded", "failed"})
        self.assertEqual(run["status"], "awaiting_decision")

        # UI form needs the CSRF token rendered on the page
        s, page = http("GET", self.base + f"/runs/{rid}", token=None, headers={"Accept": "text/html"})
        self.assertEqual(s, 200)
        self.assertIn("Delete the old rows?", page)
        csrf = page.split("name='csrf' value='")[1].split("'")[0]
        self.assertEqual(http("POST", self.base + f"/runs/{rid}/decision", token=None, form="choice=keep&csrf=nope")[0], 403)
        s, _ = http("POST", self.base + f"/runs/{rid}/decision", token=None, form=f"choice=keep&note=later&csrf={csrf}")
        self.assertEqual(s, 303)

        follow = [r for r in self.hub.list_runs() if r.get("parent_run") == rid][0]
        run = self.wait_status(follow["id"], {"succeeded", "failed"})
        self.assertEqual(run["summary"], "resumed sess-42")
        self.assertAlmostEqual(run["cost_usd"], 0.02, msg="only this run's share of the session total")
        self.assertEqual((run["input_tokens"], run["output_tokens"]), (100, 7))

    def test_agent_reported_failure(self):
        self.start_worker()
        _, body = http("POST", self.base + "/events", {"source": "ops", "type": "impossible"})
        run = self.wait_status(json.loads(body)["runs"][0], {"succeeded", "failed"})
        self.assertEqual((run["status"], run["exit_code"]), ("failed", 0))
        self.assertEqual(run["summary"], "blocked by permissions")

    def test_cwd_outside_allowlist_is_refused(self):
        self.start_worker()
        _, body = http("POST", self.base + "/events", {"source": "ops", "type": "escape"})
        run = self.wait_status(json.loads(body)["runs"][0], {"failed"})
        self.assertEqual(run["exit_code"], 126)
        self.assertIn("outside", run["summary"])

    def test_timeout_kills_process(self):
        self.start_worker()
        _, body = http("POST", self.base + "/events", {"source": "ops", "type": "slow"})
        t0 = time.time()
        run = self.wait_status(json.loads(body)["runs"][0], {"failed"})
        self.assertLess(time.time() - t0, 15)
        self.assertEqual(run["exit_code"], 124)

    def test_github_webhook_signature(self):
        payload = json.dumps({"action": "opened", "repository": {"full_name": "me/app"},
                              "pull_request": {"number": 7, "title": "Add thing", "html_url": "https://github.com/me/app/pull/7"}})
        sig = "sha256=" + hmac.new(b"gh-secret", payload.encode(), hashlib.sha256).hexdigest()
        hdr = {"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d-1", "Content-Type": "application/json"}

        def post(signature):
            req = urllib.request.Request(self.base + "/hooks/github", data=payload.encode(), method="POST",
                                         headers={**hdr, "X-Hub-Signature-256": signature})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                e.close()
                return e.code, None

        self.assertEqual(post("sha256=bad")[0], 401)
        s, body = post(sig)
        self.assertEqual(s, 200)
        self.assertEqual(body["status"], "routed")
        run = self.hub.get_run(body["runs"][0])
        self.assertEqual(run["events"][0]["subject"], "me/app#7")
        self.assertEqual(post(sig)[1]["status"], "duplicate", "same delivery id is deduped")

    def test_tunnelled_requests_are_not_trusted_as_local(self):
        page = self.base + "/"
        self.assertEqual(http("GET", page, token=None, headers={"Accept": "text/html"})[0], 200, "direct local is trusted")
        for h in ("X-Forwarded-For", "CF-Connecting-IP", "Forwarded"):
            s, _ = http("GET", page, token=None, headers={"Accept": "text/html", h: "203.0.113.9"})
            self.assertEqual(s, 303, f"{h} must force a login")
        self.hub.public_url = "https://dispatchbox.example.com"
        try:
            self.assertEqual(http("GET", page, token=None, headers={"Accept": "text/html"})[0], 303,
                             "a hub with a public URL never trusts loopback")
            self.assertEqual(http("POST", self.base + "/runs/1/decision", token=None, form="choice=x&csrf=y")[0], 401)
        finally:
            self.hub.public_url = ""

    def test_ui_requires_login_from_non_loopback_cookie(self):
        # loopback is trusted; the login form still issues a cookie for remote use
        s, _ = http("POST", self.base + "/login", token=None, form="token=wrong")
        self.assertEqual(s, 401)
        s, _ = http("POST", self.base + "/login", token=None, form=f"token={TOKEN}")
        self.assertEqual(s, 303)


class WorkerUnitTest(unittest.TestCase):
    def test_resolve_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            sub = Path(d, "a")
            sub.mkdir()
            self.assertEqual(resolve_cwd(str(sub), [d]), sub.resolve())
            with self.assertRaises(PermissionError):
                resolve_cwd(str(Path(d, "a", "..", "..")), [str(sub)])
            with self.assertRaises(FileNotFoundError):
                resolve_cwd(str(Path(d, "missing")), [d])


if __name__ == "__main__":
    unittest.main()
