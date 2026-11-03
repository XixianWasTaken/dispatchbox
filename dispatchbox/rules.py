"""Rules file loading, event normalisation, matching and templating."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cron import Cron

RUNNERS = {"claude", "codex", "shell"}
EVENT_FIELDS = ("source", "type", "subject", "summary", "link")

_DURATION = re.compile(r"^(\d+)\s*([smhd]?)$")
_TEMPLATE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.]*)\}")


def parse_duration(value: Any, default: int) -> int:
    """Turn 90, "90", "90s", "20m", "2h" or "1d" into seconds."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return int(value)
    m = _DURATION.match(str(value).strip())
    if not m:
        raise ValueError(f"bad duration {value!r}")
    n, unit = int(m.group(1)), m.group(2) or "s"
    return n * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def render(template: str, ctx: dict[str, Any]) -> str:
    """Replace {field} and {payload.a.b} with values from ctx; unknown names are left as-is."""

    def repl(m: re.Match[str]) -> str:
        cur: Any = ctx
        for key in m.group(1).split("."):
            if isinstance(cur, dict) and key in cur:
                cur = cur[key]
            else:
                return m.group(0)
        if isinstance(cur, (dict, list)):
            return json.dumps(cur, ensure_ascii=False)
        return "" if cur is None else str(cur)

    return _TEMPLATE.sub(repl, template)


def normalize_event(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("event must be a JSON object")
    for key in ("source", "type"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            raise ValueError(f"event.{key} is required")
    event = {k: (str(raw[k]) if raw.get(k) is not None else "") for k in EVENT_FIELDS}
    payload = raw.get("payload", {})
    event["payload"] = payload if payload is not None else {}
    key = raw.get("dedupe_key")
    if not key:
        if event["subject"]:
            key = f"{event['source']}:{event['type']}:{event['subject']}"
        else:
            blob = json.dumps({k: event[k] for k in (*EVENT_FIELDS, "payload")}, sort_keys=True, ensure_ascii=False)
            key = f"{event['source']}:{event['type']}:sha1:{hashlib.sha1(blob.encode()).hexdigest()[:16]}"
    event["dedupe_key"] = str(key)
    return event


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [str(v) for v in value]


@dataclass
class Rule:
    name: str
    sources: list[str]
    types: list[str]
    subjects: list[str]
    worker: str
    runner: str
    cwd: str
    prompt: str
    model: str | None
    skill: str | None
    group: str
    concurrency: int
    coalesce: bool
    timeout: int
    notify: str
    args: list[str]
    command: str | None
    cont: bool
    retries: int = 0

    def matches(self, event: dict[str, Any]) -> bool:
        def ok(patterns: list[str], value: str) -> bool:
            return not patterns or any(fnmatch.fnmatchcase(value, p) for p in patterns)

        return ok(self.sources, event["source"]) and ok(self.types, event["type"]) and ok(self.subjects, event["subject"])

    def to_spec(self) -> dict[str, Any]:
        return {
            "rule": self.name,
            "runner": self.runner,
            "cwd": self.cwd,
            "prompt": self.prompt,
            "model": self.model,
            "skill": self.skill,
            "timeout": self.timeout,
            "notify": self.notify,
            "args": self.args,
            "command": self.command,
            "retries": self.retries,
        }


@dataclass
class Schedule:
    name: str
    cron: Cron
    event: dict[str, Any]


@dataclass
class Config:
    rules: list[Rule]
    schedules: list[Schedule]
    dedupe_window: int
    memory: list[str] = field(default_factory=list)
    require_decision_for: list[str] = field(default_factory=list)
    chat: dict[str, Any] = field(default_factory=dict)

    def match(self, event: dict[str, Any]) -> list[Rule]:
        hits: list[Rule] = []
        for rule in self.rules:
            if rule.matches(event):
                hits.append(rule)
                if not rule.cont:
                    break
        return hits


def _build_rule(raw: dict[str, Any], defaults: dict[str, Any]) -> Rule:
    merged = {**defaults, **raw}
    name = merged.get("name")
    if not name:
        raise ValueError("every rule needs a name")
    when = merged.get("when", {}) or {}
    runner = merged.get("runner", "claude")
    if runner not in RUNNERS:
        raise ValueError(f"rule {name!r}: runner must be one of {sorted(RUNNERS)}")
    if runner == "shell" and not merged.get("command"):
        raise ValueError(f"rule {name!r}: shell runner needs a command")
    if runner != "shell" and not merged.get("prompt"):
        raise ValueError(f"rule {name!r}: {runner} runner needs a prompt")
    concurrency = int(merged.get("concurrency", 1))
    if concurrency < 1:
        raise ValueError(f"rule {name!r}: concurrency must be >= 1")
    retries = int(merged.get("retries", 0))
    if retries < 0:
        raise ValueError(f"rule {name!r}: retries must be >= 0")
    notify = merged.get("notify", "on_decision")
    if notify not in {"never", "on_decision", "on_failure", "always"}:
        raise ValueError(f"rule {name!r}: bad notify {notify!r}")
    return Rule(
        name=name,
        sources=_as_list(when.get("source")),
        types=_as_list(when.get("types", when.get("type"))),
        subjects=_as_list(when.get("subject")),
        worker=str(merged.get("worker", "*")),
        runner=runner,
        cwd=str(merged.get("cwd", ".")),
        prompt=str(merged.get("prompt", "")),
        model=merged.get("model"),
        skill=merged.get("skill"),
        group=str(merged.get("group", "{rule}:{subject}")),
        concurrency=concurrency,
        coalesce=bool(merged.get("coalesce", True)),
        timeout=parse_duration(merged.get("timeout"), 1800),
        notify=notify,
        args=[str(a) for a in merged.get("args", [])],
        command=merged.get("command"),
        cont=bool(merged.get("continue", False)),
        retries=retries,
    )


def load_config(data: dict[str, Any]) -> Config:
    defaults = data.get("defaults", {}) or {}
    rules = [_build_rule(r, defaults) for r in data.get("rules", [])]
    names = [r.name for r in rules]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ValueError(f"duplicate rule names: {sorted(dupes)}")
    schedules = []
    for s in data.get("schedules", []):
        if not s.get("name") or not s.get("cron"):
            raise ValueError("every schedule needs a name and a cron expression")
        event = {"source": "cron", "type": s["name"], "subject": s["name"], **(s.get("event") or {})}
        schedules.append(Schedule(name=s["name"], cron=Cron(s["cron"]), event=event))
    return Config(
        rules=rules,
        schedules=schedules,
        dedupe_window=parse_duration(data.get("dedupe_window"), 600),
        memory=_as_list(data.get("memory")),
        require_decision_for=_as_list(data.get("require_decision_for")),
        chat=_load_chat(data.get("chat")),
    )


def _load_chat(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("chat must be an object")
    for platform, conf in raw.items():
        if platform not in {"feishu", "slack"}:
            raise ValueError(f"chat.{platform}: only feishu and slack are supported")
        if not isinstance(conf, dict):
            raise ValueError(f"chat.{platform} must be an object")
        for key in ("allowed_users", "approvers"):
            if key in conf and not (isinstance(conf[key], list) and all(isinstance(u, str) for u in conf[key])):
                raise ValueError(f"chat.{platform}.{key} must be a list of user ids")
    return raw


def load_config_file(path: str | Path) -> Config:
    with open(path, encoding="utf-8") as f:
        return load_config(json.load(f))
