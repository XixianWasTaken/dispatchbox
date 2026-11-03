import json
import tempfile
import time
import threading
import unittest
from datetime import datetime
from pathlib import Path

from dispatchbox.hub import Hub, HubError, build_prompt, extract_decision, extract_summary
from tests.helpers import BASIC, FakeClock, bump_mtime, write_rules


class HubTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rules = write_rules(self.tmp.name, BASIC)
        self.clock = FakeClock()
        self.hub = Hub(self.rules, str(Path(self.tmp.name, "b.db")), lost_after=60, clock=self.clock)
        self.notes = []
        self.hub.notifier = lambda run, link: self.notes.append((run["id"], run["status"], link))

    def tearDown(self):
        self.hub.close()
        self.tmp.cleanup()

    def pr(self, n, **kw):
        return {"source": "github", "type": "pr.opened", "subject": f"repo#{n}", "summary": f"PR {n}", **kw}

    def test_route_and_claim_builds_prompt(self):
        r = self.hub.ingest(self.pr(1, payload={"head_sha": "abc"}))
        self.assertEqual(r["status"], "routed")
        task = self.hub.claim("m5")
        self.assertEqual(task["run_id"], r["runs"][0])
        self.assertIn("Review repo#1: PR 1", task["prompt"])
        self.assertIn("abc", task["prompt"])
        self.assertIn("DECISIONS.md", task["prompt"])
        self.assertIn("delete_data", task["prompt"])
        self.assertIn("DISPATCHBOX_DECISION:", task["prompt"])
        self.assertIsNone(self.hub.claim("m5"))

    def test_unmatched(self):
        r = self.hub.ingest({"source": "nobody", "type": "x"})
        self.assertEqual(r["status"], "unmatched")
        self.assertEqual(self.hub.list_runs(), [])

    def test_bad_event(self):
        with self.assertRaises(HubError) as cm:
            self.hub.ingest({"type": "x"})
        self.assertEqual(cm.exception.status, 400)

    def test_dedupe_window(self):
        self.hub.ingest(self.pr(1))
        self.assertEqual(self.hub.ingest(self.pr(1))["status"], "duplicate")
        self.clock.advance(601)
        # the earlier run is still queued, so the new event joins it instead of opening a second run
        self.assertEqual(self.hub.ingest(self.pr(1))["status"], "coalesced")
        self.assertEqual(len(self.hub.list_runs()), 1)

    def test_coalesce_merges_follow_ups_into_queued_run(self):
        a = self.hub.ingest({"source": "hub", "type": "feedback.new", "subject": "#12", "summary": "first"})
        b = self.hub.ingest({"source": "hub", "type": "feedback.followup", "subject": "#12", "summary": "more"})
        self.assertEqual(b["status"], "coalesced")
        self.assertEqual(b["coalesced_into"], a["runs"])
        task = self.hub.claim("m4")
        self.assertEqual(len(task["events"]), 2)
        self.assertIn("2 related events were merged", task["prompt"])

    def test_group_runs_one_at_a_time(self):
        self.hub.ingest({"source": "hub", "type": "feedback.new", "subject": "#12"})
        first = self.hub.claim("m4")
        # a follow-up arriving while the first run is busy queues a new run in the same group
        r = self.hub.ingest({"source": "hub", "type": "feedback.followup", "subject": "#12"})
        self.assertEqual(r["status"], "routed")
        self.assertIsNone(self.hub.claim("m4"), "same group must wait")
        self.hub.ingest({"source": "hub", "type": "feedback.new", "subject": "#13"})
        other = self.hub.claim("m4")
        self.assertEqual(other["events"][0]["subject"], "#13")
        self.hub.finish(first["run_id"], "m4", {"exit_code": 0, "output": "done"})
        nxt = self.hub.claim("m4")
        self.assertEqual(nxt["events"][0]["type"], "feedback.followup")

    def test_rule_concurrency(self):
        for n in range(3):
            self.hub.ingest(self.pr(n))
        self.assertIsNotNone(self.hub.claim("w"))
        self.assertIsNotNone(self.hub.claim("w"))
        self.assertIsNone(self.hub.claim("w"), "concurrency 2 reached")

    def test_worker_pattern_and_runner_filter(self):
        self.hub.ingest({"source": "hub", "type": "feedback.new", "subject": "#1"})
        self.assertIsNone(self.hub.claim("m5"))
        self.assertIsNone(self.hub.claim("m4", runners=["codex"]))
        self.assertIsNotNone(self.hub.claim("m4", runners=["claude"]))

    def test_long_poll_wakes_on_new_event(self):
        got = {}
        t = threading.Thread(target=lambda: got.update(task=self.hub.claim("w", wait=5)))
        t.start()
        time.sleep(0.2)
        self.hub.ingest(self.pr(9))
        t.join(3)
        self.assertFalse(t.is_alive())
        self.assertEqual(got["task"]["events"][0]["subject"], "repo#9")

    def test_finish_summary_and_ownership(self):
        self.hub.ingest(self.pr(1))
        task = self.hub.claim("w")
        with self.assertRaises(HubError) as cm:
            self.hub.finish(task["run_id"], "someone-else", {"exit_code": 0})
        self.assertEqual(cm.exception.status, 409)
        r = self.hub.finish(task["run_id"], "w", {"exit_code": 0, "output": "work\nDISPATCHBOX_SUMMARY: all good"})
        self.assertEqual(r["status"], "succeeded")
        self.assertEqual(self.hub.get_run(task["run_id"])["summary"], "all good")
        self.assertEqual(self.notes, [], "notify=on_decision should stay quiet on success")

    def test_decision_round_trip(self):
        self.hub.ingest(self.pr(1))
        task = self.hub.claim("w")
        out = 'found stale table\nDISPATCHBOX_DECISION: {"question": "Drop table x?", "options": ["drop", "keep"]}\nDISPATCHBOX_SUMMARY: waiting'
        r = self.hub.finish(task["run_id"], "w", {"exit_code": 0, "output": out, "session_id": "sess-1"})
        self.assertEqual(r["status"], "awaiting_decision")
        self.assertEqual(self.notes[0][:2], (task["run_id"], "awaiting_decision"))
        with self.assertRaises(HubError):
            self.hub.decide(task["run_id"], "maybe")
        d = self.hub.decide(task["run_id"], "keep", note="we still need it")
        follow_id = d["follow_up"]["runs"][0]
        follow = self.hub.claim("w")
        self.assertEqual(follow["run_id"], follow_id)
        self.assertEqual(follow["resume_session"], "sess-1")
        self.assertIn("The owner chose keep", follow["prompt"])
        self.assertIn(f"continues run #{task['run_id']}", follow["prompt"])
        with self.assertRaises(HubError):
            self.hub.decide(task["run_id"], "keep")

    def test_reap_lost_runs(self):
        self.hub.ingest(self.pr(1))
        task = self.hub.claim("w")
        self.clock.advance(30)
        self.hub.heartbeat(task["run_id"], "w")
        self.clock.advance(59)
        self.assertEqual(self.hub.reap(), [])
        self.clock.advance(2)
        self.assertEqual(self.hub.reap(), [task["run_id"]])
        with self.assertRaises(HubError):
            self.hub.heartbeat(task["run_id"], "w")

    def test_cron_tick_once_per_minute(self):
        r = self.hub.tick_cron(datetime(2026, 11, 2, 18, 0, 5))
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0]["status"], "unmatched")  # no rule for cron/nightly in BASIC
        self.assertEqual(self.hub.tick_cron(datetime(2026, 11, 2, 18, 0, 40)), [])
        self.assertEqual(self.hub.tick_cron(datetime(2026, 11, 2, 18, 1)), [])

    def test_reload_rules_and_keep_last_good(self):
        data = json.loads(self.rules.read_text())
        data["rules"].append({"name": "nightly", "when": {"source": "cron"}, "prompt": "Clean up", "runner": "claude"})
        self.rules.write_text(json.dumps(data))
        bump_mtime(self.rules)
        r = self.hub.tick_cron(datetime(2026, 11, 3, 18, 0))
        self.assertEqual(r[0]["status"], "routed")
        self.rules.write_text("{ not json")
        bump_mtime(self.rules)
        self.hub.reload_if_changed()
        self.assertEqual(len(self.hub.config.rules), 4)


class ContractTest(unittest.TestCase):
    def test_extractors(self):
        self.assertEqual(extract_summary("a\nb\nDISPATCHBOX_SUMMARY: done\n"), "done")
        self.assertEqual(extract_summary("line one\nlast line"), "last line")
        self.assertEqual(extract_decision('x\nDISPATCHBOX_DECISION: {"question":"q?","options":["a"]}')["options"], ["a"])
        self.assertEqual(extract_decision("DISPATCHBOX_DECISION: should I?")["question"], "should I?")
        self.assertIsNone(extract_decision("nothing"))

    def test_payload_truncated(self):
        spec = {"rule": "r", "prompt": "p"}
        events = [{"id": 1, "source": "s", "type": "t", "subject": "", "summary": "", "link": "", "payload": {"x": "y" * 9000}}]
        self.assertIn("(truncated)", build_prompt(1, spec, events, None))


if __name__ == "__main__":
    unittest.main()
