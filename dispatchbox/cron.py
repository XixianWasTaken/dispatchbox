"""Minimal 5-field cron matcher: minute hour day-of-month month day-of-week.

Supports `*`, `*/n`, `a`, `a-b`, `a-b/n` and comma lists. Day-of-week 0 and 7 are Sunday.
When both day fields are restricted, a time matches if either one matches (classic cron rule).
"""

from __future__ import annotations

from datetime import datetime

_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]


def _parse_field(text: str, lo: int, hi: int) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError(f"bad step in {text!r}")
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = int(part)
            end = hi if step != 1 else start
        if start < lo or end > hi or start > end:
            raise ValueError(f"value out of range in {text!r}")
        values.update(range(start, end + 1, step))
    return values


class Cron:
    def __init__(self, expr: str):
        fields = expr.split()
        if len(fields) != 5:
            raise ValueError(f"cron needs 5 fields, got {expr!r}")
        self.expr = expr
        parsed = [_parse_field(f, lo, hi) for f, (lo, hi) in zip(fields, _RANGES)]
        self.minutes, self.hours, self.days, self.months, dows = parsed
        self.dows = {d % 7 for d in dows}
        self.day_restricted = fields[2] != "*"
        self.dow_restricted = fields[4] != "*"

    def matches(self, t: datetime) -> bool:
        if t.minute not in self.minutes or t.hour not in self.hours or t.month not in self.months:
            return False
        dom_ok = t.day in self.days
        dow_ok = (t.isoweekday() % 7) in self.dows
        if self.day_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok
