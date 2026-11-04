"""Explicit status verdicts, usage accounting, retries of lost runs, and schema migration."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dispatchbox import store
from dispatchbox.hub import Hub, extract_status
from dispatchbox.rules import load_config
from dispatchbox.worker import WorkerConfig, build_command, parse_output
from tests.helpers import FakeClock, write_rules

RULES = {
    "rules": [
        {"name": "job", "when": {"source": "s"}, "prompt": "do {subject}", "group": "{subject}"},
        {"name": "retry-job", "when": {"source": "r"}, "prompt": "do {subject}", "retries": 1, "notify": "on_failure"},
        {"name": "after", "when": {"source": "dispatchbox", "type": "decision.made"}, "prompt": "go on"},
    ]
}
CODEX_RULES = {
    "rules": [
        {"name": "job", "when": {"source": "s"}, "runner": "codex", "prompt": "do {subject}"},
        {"name": "after", "when": {"source": "dispatchbox", "type": "decision.made"}, "runner": "codex", "prompt": "go on"},
    ]
}
ASK = 'DISPATCHBOX_DECISION: {"question": "Go on?", "options": ["yes", "no"]}'

# Shapes copied from real `claude -p --output-format json` and `codex exec --json` output.
CLAUDE_OUT = json.dumps({"type": "result", "is_error": False, "session_id": "s-1", "total_cost_usd": 0.0024359,
                         "result": "ok", "usage": {"input_tokens": 2, "cache_creation_input_tokens": 10973,
                                                   "cache_read_input_tokens": 23910, "output_tokens": 4}})
CODEX_OK = "\n".join([
    '{"type":"thread.started","thread_id":"t-1"}',
    '{"type":"item.completed","item":{"id":"item_0","type":"error","message":"config warning"}}',
    '{"type":"turn.completed","usage":{"input_tokens":23162,"cached_input_tokens":13184,"output_tokens":5}}',
])
CODEX_FAILED = "\n".join([
    '{"type":"thread.started","thread_id":"t-2"}',
    '{"type":"turn.started"}',
    '{"type":"error","message":"model not supported"}',
    '{"type":"turn.failed","error":{"message":"The model is not supported"}}',
])


# Schema of the first pre-release version, before the usage and retry columns.
V01_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    received_at REAL NOT NULL,
    source TEXT NOT NULL,
    type TEXT NOT NULL,
    subject TEXT NOT NULL,
    summary TEXT NOT NULL,
    link TEXT NOT NULL,
    payload TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    status TEXT NOT NULL,          -- routed | coalesced | duplicate | unmatched
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_dedupe ON events(dedupe_key, received_at);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    rule TEXT NOT NULL,
    worker_pattern TEXT NOT NULL,
    group_key TEXT NOT NULL,
    concurrency INTEGER NOT NULL,
    spec TEXT NOT NULL,
    status TEXT NOT NULL,          -- queued | running | succeeded | failed | lost | awaiting_decision | decided
    created_at REAL NOT NULL,
    claimed_by TEXT,
    claimed_at REAL,
    heartbeat_at REAL,
    finished_at REAL,
    exit_code INTEGER,
    summary TEXT,
    session_id TEXT,
    prompt TEXT,
    output_tail TEXT,
    decision_request TEXT,
    decision TEXT,
    parent_run INTEGER
);
CREATE INDEX IF NOT EXISTS runs_status ON runs(status, id);

CREATE TABLE IF NOT EXISTS run_events (
    run_id INTEGER NOT NULL,
    event_id INTEGER NOT NULL,
    PRIMARY KEY (run_id, event_id)
);
"""


class Step0Hub(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clock = FakeClock()
        self.hub = Hub(write_rules(self.tmp.name, RULES), str(Path(self.tmp.name, "b.db")), lost_after=60,
                       clock=self.clock)
        self.notes = []
        self.hub.notifier = lambda run, link: self.notes.append((run["id"], run["status"]))

    def tearDown(self):
        self.hub.close()
        self.tmp.cleanup()

    def run_once(self, output, exit_code=0, **extra):
        self.hub.ingest({"source": "s", "type": "t", "subject": f"x{len(self.hub.list_runs())}"})
        task = self.hub.claim("w")
        self.hub.finish(task["run_id"], "w", {"exit_code": exit_code, "output": output, **extra})
        return self.hub.get_run(task["run_id"])

    def continue_session(self, run_id, output="done", **extra):
        """Answer the decision run_id waits for, then finish the follow-up run with the given result."""
        follow = self.hub.decide(run_id, "yes")["follow_up"]["runs"][0]
        task = self.hub.claim("w")
        self.assertEqual((task["run_id"], task["resume_session"]), (follow, self.hub.get_run(run_id)["session_id"]))
        self.hub.finish(follow, "w", {"exit_code": 0, "output": output, **extra})
        return self.hub.get_run(follow)

    def test_continued_claude_session_records_only_its_own_cost(self):
        # Figures from a real claude 2.1.310 session: the resumed call reports the session's running total cost,
        # while its token counts cover that call alone.
        first = self.run_once(ASK, session_id="s-1", cost_usd=0.72866, input_tokens=36424, output_tokens=4)
        second = self.continue_session(first["id"], ASK, session_id="s-1", cost_usd=0.895048,
                                       input_tokens=36466, output_tokens=4)
        self.assertAlmostEqual(second["cost_usd"], 0.166388)
        self.assertEqual((second["input_tokens"], second["output_tokens"]), (36466, 4))
        third = self.continue_session(second["id"], session_id="s-1", cost_usd=1.0, input_tokens=100, output_tokens=2)
        self.assertAlmostEqual(third["cost_usd"], 0.104952, msg="the whole chain counts, not just the parent")
        self.assertAlmostEqual(first["cost_usd"], 0.72866, msg="the first run is left alone")

    def test_continued_codex_session_records_only_its_own_tokens(self):
        # Figures from a real codex 0.166 session: the resumed call reports the session's running token totals.
        self.hub.close()
        self.hub = Hub(write_rules(self.tmp.name, CODEX_RULES), str(Path(self.tmp.name, "c.db")), lost_after=60,
                       clock=self.clock)
        first = self.run_once(ASK, session_id="t-1", input_tokens=23372, output_tokens=5)
        second = self.continue_session(first["id"], session_id="t-1", input_tokens=50928, output_tokens=10)
        self.assertEqual((second["input_tokens"], second["output_tokens"]), (27556, 5))
        self.assertIsNone(second["cost_usd"])

    def test_usage_of_a_new_session_or_below_the_total_is_kept(self):
        first = self.run_once(ASK, session_id="s-1", cost_usd=0.5)
        fresh = self.continue_session(first["id"], session_id="s-2", cost_usd=0.4)
        self.assertAlmostEqual(fresh["cost_usd"], 0.4, msg="a new session owes nothing to the old one")
        first = self.run_once(ASK, session_id="s-3", cost_usd=0.5)
        same = self.continue_session(first["id"], session_id="s-3", cost_usd=0.1)
        self.assertAlmostEqual(same["cost_usd"], 0.1, msg="a figure below the running total is per call")

    def test_agent_failed_verdict_beats_clean_exit(self):
        run = self.run_once("tried\nDISPATCHBOX_STATUS: failed\nDISPATCHBOX_SUMMARY: permission denied, file kept")
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["summary"], "permission denied, file kept")

    def test_succeeded_verdict_and_missing_verdict(self):
        self.assertEqual(self.run_once("DISPATCHBOX_STATUS: succeeded\nDISPATCHBOX_SUMMARY: done")["status"], "succeeded")
        self.assertEqual(self.run_once("no verdict at all")["status"], "succeeded")
        self.assertEqual(self.run_once("DISPATCHBOX_STATUS: succeeded", exit_code=1)["status"], "failed")

    def test_status_field_in_result(self):
        self.assertEqual(self.run_once("fine", status="failed")["status"], "failed")

    def test_prompt_asks_for_verdict(self):
        self.hub.ingest({"source": "s", "type": "t", "subject": "p"})
        self.assertIn("DISPATCHBOX_STATUS: succeeded | failed", self.hub.claim("w")["prompt"])

    def test_usage_is_stored(self):
        run = self.run_once("ok", cost_usd=0.0024359, input_tokens=34885, output_tokens=4)
        self.assertAlmostEqual(run["cost_usd"], 0.0024359)
        self.assertEqual((run["input_tokens"], run["output_tokens"]), (34885, 4))
        bad = self.run_once("ok", cost_usd="n/a", input_tokens=None)
        self.assertIsNone(bad["cost_usd"])

    def test_lost_run_retries_then_gives_up(self):
        r = self.hub.ingest({"source": "r", "type": "t", "subject": "a", "payload": {"k": 1}})
        first = self.hub.claim("w")
        self.clock.advance(61)
        self.assertEqual(self.hub.reap(), [first["run_id"]])
        self.assertEqual(self.notes, [], "no alert while a retry is pending")
        lost = self.hub.get_run(first["run_id"])
        self.assertEqual(lost["status"], "lost")
        self.assertIn("retrying as run #", lost["summary"])

        second = self.hub.claim("w2")
        self.assertNotEqual(second["run_id"], first["run_id"])
        self.assertEqual([e["id"] for e in second["events"]], [r["event_id"]])
        self.assertIn("This is attempt 2", second["prompt"])
        self.assertIn(f"run #{first['run_id']}", second["prompt"])
        run2 = self.hub.get_run(second["run_id"])
        self.assertEqual((run2["attempt"], run2["retry_of"]), (2, first["run_id"]))

        self.clock.advance(61)
        self.assertEqual(self.hub.reap(), [second["run_id"]])
        self.assertIsNone(self.hub.claim("w"), "retries exhausted")
        self.assertEqual(self.notes, [(second["run_id"], "lost")])

    def test_no_retry_by_default(self):
        self.hub.ingest({"source": "s", "type": "t", "subject": "z"})
        task = self.hub.claim("w")
        self.clock.advance(61)
        self.hub.reap()
        self.assertIsNone(self.hub.claim("w"))


class Step0Units(unittest.TestCase):
    def test_extract_status(self):
        self.assertEqual(extract_status("x\nDISPATCHBOX_STATUS: **Failed**"), "failed")
        self.assertEqual(extract_status("DISPATCHBOX_STATUS: done"), "succeeded")
        self.assertIsNone(extract_status("DISPATCHBOX_STATUS: maybe"))
        self.assertIsNone(extract_status("nothing"))

    def test_retries_validation(self):
        with self.assertRaises(ValueError):
            load_config({"rules": [{"name": "a", "prompt": "p", "retries": -1}]})
        self.assertEqual(load_config({"rules": [{"name": "a", "prompt": "p"}]}).rules[0].retries, 0)

    def test_parse_claude_usage(self):
        with tempfile.TemporaryDirectory() as d:
            out = parse_output("claude", CLAUDE_OUT, "", d)
        self.assertEqual(out["input_tokens"], 2 + 10973 + 23910)
        self.assertEqual(out["output_tokens"], 4)
        self.assertEqual(out["session_id"], "s-1")

    def test_parse_claude_verbose_array(self):
        # With verbose on, claude 2.1.310 prints every message as one JSON array on a single line.
        line = json.dumps([{"type": "system", "subtype": "init"}, {"type": "assistant"}, {"type": "rate_limit_event"},
                           json.loads(CLAUDE_OUT)])
        with tempfile.TemporaryDirectory() as d:
            out = parse_output("claude", line + "\n", "", d)
        self.assertEqual((out["output"], out["session_id"], out["cost_usd"]), ("ok", "s-1", 0.0024359))
        with tempfile.TemporaryDirectory() as d:
            none = parse_output("claude", '[{"type": "system"}]\n', "", d)
        self.assertNotIn("session_id", none, "an array without a result is read as raw output")

    def test_parse_codex_success_and_failure(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "last_message.txt").write_text("ok\n")
            ok = parse_output("codex", CODEX_OK, "", d)
        self.assertEqual((ok["output"], ok["session_id"], ok["input_tokens"], ok["output_tokens"]),
                         ("ok", "t-1", 23162, 5))
        self.assertNotIn("agent_error", ok)
        with tempfile.TemporaryDirectory() as d:
            bad = parse_output("codex", CODEX_FAILED, "", d)
        self.assertTrue(bad["agent_error"])
        self.assertIn("The model is not supported", bad["output"])

    def test_codex_resume_passes_the_sandbox_as_config(self):
        cfg = WorkerConfig(hub="", name="w", token="t")
        task = {"runner": "codex", "prompt": "p", "args": ["-s", "workspace-write", "--sandbox=read-only", "--skip-git-repo-check"]}
        with tempfile.TemporaryDirectory() as d:
            fresh, _ = build_command(task, cfg, d)
            resumed, _ = build_command({**task, "resume_session": "t-1"}, cfg, d)
        self.assertIn("-s", fresh)  # a new session takes the flag as written
        self.assertEqual(resumed[:4], ["codex", "exec", "resume", "t-1"])
        self.assertNotIn("-s", resumed)
        self.assertNotIn("--sandbox=read-only", resumed)
        self.assertIn('sandbox_mode="workspace-write"', resumed)
        self.assertIn('sandbox_mode="read-only"', resumed)
        self.assertIn("--skip-git-repo-check", resumed)

    def test_migrates_old_database(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d, "old.db"))
            old = sqlite3.connect(path)
            old.executescript(V01_SCHEMA)
            old.execute("INSERT INTO runs (rule, worker_pattern, group_key, concurrency, spec, status, created_at) "
                        "VALUES ('legacy', '*', 'g', 1, '{}', 'succeeded', 0)")
            old.commit()
            old.close()
            conn = store.connect(path)
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(runs)")}
            self.assertTrue({"cost_usd", "input_tokens", "output_tokens", "attempt", "retry_of"} <= cols)
            self.assertEqual(conn.execute("SELECT attempt FROM runs").fetchone()["attempt"], 1)
            conn.close()


if __name__ == "__main__":
    unittest.main()
