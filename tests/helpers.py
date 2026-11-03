import json
import os
import tempfile
from pathlib import Path


class FakeClock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def write_rules(dirpath: str, data: dict) -> Path:
    p = Path(dirpath, "rules.json")
    p.write_text(json.dumps(data))
    return p


def bump_mtime(p: Path) -> None:
    st = p.stat()
    os.utime(p, (st.st_atime, st.st_mtime + 5))


BASIC = {
    "dedupe_window": "10m",
    "memory": ["DECISIONS.md"],
    "require_decision_for": ["delete_data"],
    "rules": [
        {"name": "review", "when": {"source": "github", "types": ["pr.*"]}, "runner": "claude",
         "prompt": "Review {subject}: {summary}", "concurrency": 2},
        {"name": "feedback", "when": {"source": "hub", "type": "feedback.*"}, "runner": "claude",
         "prompt": "Handle {subject}", "worker": "m4", "group": "fb:{subject}", "concurrency": 3},
        {"name": "after-decision", "when": {"source": "dispatchbox", "type": "decision.made"}, "runner": "claude",
         "prompt": "The owner chose {payload.choice}. Continue."},
    ],
    "schedules": [{"name": "nightly", "cron": "0 18 * * *"}],
}
