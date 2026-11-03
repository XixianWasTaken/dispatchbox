import unittest
from datetime import datetime

from dispatchbox.cron import Cron
from dispatchbox.rules import load_config, normalize_event, parse_duration, render


class CronTest(unittest.TestCase):
    def test_basic_fields(self):
        c = Cron("*/15 9-17 * * 1-5")
        self.assertTrue(c.matches(datetime(2026, 11, 2, 9, 30)))   # Monday
        self.assertFalse(c.matches(datetime(2026, 11, 2, 9, 31)))
        self.assertFalse(c.matches(datetime(2026, 11, 1, 9, 30)))  # Sunday
        self.assertFalse(c.matches(datetime(2026, 11, 2, 18, 0)))

    def test_sunday_is_0_and_7(self):
        for expr in ("0 8 * * 0", "0 8 * * 7"):
            self.assertTrue(Cron(expr).matches(datetime(2026, 11, 1, 8, 0)), expr)

    def test_dom_or_dow_when_both_restricted(self):
        c = Cron("0 0 1 * 1")
        self.assertTrue(c.matches(datetime(2026, 12, 1, 0, 0)))   # 1st, a Tuesday
        self.assertTrue(c.matches(datetime(2026, 11, 2, 0, 0)))   # a Monday
        self.assertFalse(c.matches(datetime(2026, 11, 3, 0, 0)))

    def test_lists_and_stepped_start(self):
        c = Cron("5,10 0/6 * * *")
        self.assertTrue(c.matches(datetime(2026, 1, 1, 12, 10)))
        self.assertFalse(c.matches(datetime(2026, 1, 1, 13, 10)))

    def test_rejects_bad(self):
        for bad in ("* * * *", "60 * * * *", "*/0 * * * *", "5-1 * * * *"):
            with self.assertRaises(ValueError, msg=bad):
                Cron(bad)


class RulesTest(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(parse_duration("20m", 0), 1200)
        self.assertEqual(parse_duration(90, 0), 90)
        self.assertEqual(parse_duration(None, 7), 7)
        with self.assertRaises(ValueError):
            parse_duration("soon", 0)

    def test_render(self):
        ctx = {"subject": "a#1", "payload": {"x": {"y": 3}, "list": [1]}}
        self.assertEqual(render("{subject} {payload.x.y} {payload.list} {missing}", ctx), "a#1 3 [1] {missing}")

    def test_normalize_and_default_dedupe(self):
        e = normalize_event({"source": "s", "type": "t", "subject": "x"})
        self.assertEqual(e["dedupe_key"], "s:t:x")
        a = normalize_event({"source": "s", "type": "t", "payload": {"a": 1}})
        b = normalize_event({"source": "s", "type": "t", "payload": {"a": 2}})
        self.assertNotEqual(a["dedupe_key"], b["dedupe_key"])
        with self.assertRaises(ValueError):
            normalize_event({"source": "s"})

    def test_first_match_and_continue(self):
        cfg = load_config({"rules": [
            {"name": "a", "when": {"source": "x"}, "prompt": "p", "continue": True},
            {"name": "b", "when": {"source": "x", "types": ["t*"]}, "prompt": "p"},
            {"name": "c", "when": {"source": "x"}, "prompt": "p"},
        ]})
        ev = normalize_event({"source": "x", "type": "t1"})
        self.assertEqual([r.name for r in cfg.match(ev)], ["a", "b"])
        ev2 = normalize_event({"source": "x", "type": "other"})
        self.assertEqual([r.name for r in cfg.match(ev2)], ["a", "c"])

    def test_validation(self):
        bad = [
            {"rules": [{"when": {}, "prompt": "p"}]},
            {"rules": [{"name": "a", "runner": "bash", "prompt": "p"}]},
            {"rules": [{"name": "a", "runner": "shell"}]},
            {"rules": [{"name": "a"}]},
            {"rules": [{"name": "a", "prompt": "p"}, {"name": "a", "prompt": "p"}]},
            {"rules": [{"name": "a", "prompt": "p", "notify": "loud"}]},
            {"schedules": [{"name": "x", "cron": "bad"}]},
        ]
        for data in bad:
            with self.assertRaises(ValueError, msg=data):
                load_config(data)

    def test_defaults_merge(self):
        cfg = load_config({"defaults": {"worker": "m4", "timeout": "5m"}, "rules": [{"name": "a", "prompt": "p"}]})
        self.assertEqual(cfg.rules[0].worker, "m4")
        self.assertEqual(cfg.rules[0].timeout, 300)


if __name__ == "__main__":
    unittest.main()
