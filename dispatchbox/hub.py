"""Hub logic: ingest events, route them to runs, hand runs to workers, collect results.

This module has no HTTP code so it can be tested directly; see server.py for the HTTP layer.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import store
from .net import ssl_context
from .rules import Config, load_config_file, normalize_event, render

log = logging.getLogger("dispatchbox.hub")

MAX_PAYLOAD_IN_PROMPT = 4000
DECISION_MARK = "DISPATCHBOX_DECISION:"
SUMMARY_MARK = "DISPATCHBOX_SUMMARY:"
STATUS_MARK = "DISPATCHBOX_STATUS:"
TERMINAL = {"succeeded", "failed", "lost", "decided"}


class HubError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Hub:
    def __init__(
        self,
        rules_path: str | Path,
        db_path: str = "dispatchbox.db",
        lost_after: int = 120,
        public_url: str = "",
        clock: Callable[[], float] = time.time,
    ):
        self.rules_path = Path(rules_path)
        self.lost_after = lost_after
        self.public_url = public_url.rstrip("/")
        self.clock = clock
        self.lock = threading.RLock()
        self.cv = threading.Condition(self.lock)
        self.db = store.connect(db_path)
        self._rules_mtime = 0.0
        self.config: Config = load_config_file(self.rules_path)
        self._rules_mtime = self.rules_path.stat().st_mtime
        self._last_cron_minute: str | None = None
        self.notifier: Callable[[dict[str, Any], str], None] = self._default_notify
        self.adapters: dict[str, Any] = {}   # name -> ChatAdapter, see chat.py
        self.background: Callable[..., None] = _background

    def close(self) -> None:
        with self.lock:
            self.db.close()

    # ------------------------------------------------------------------ config

    def reload_if_changed(self) -> None:
        try:
            mtime = self.rules_path.stat().st_mtime
        except FileNotFoundError:
            return
        if mtime == self._rules_mtime:
            return
        try:
            cfg = load_config_file(self.rules_path)
        except Exception as e:  # keep the last good config
            log.error("rules file invalid, keeping previous version: %s", e)
            self._rules_mtime = mtime
            return
        with self.lock:
            self.config = cfg
            self._rules_mtime = mtime
        log.info("rules reloaded: %d rules, %d schedules", len(cfg.rules), len(cfg.schedules))

    # ------------------------------------------------------------------ ingest

    def ingest(self, raw: dict[str, Any], parent_run: int | None = None) -> dict[str, Any]:
        try:
            event = normalize_event(raw)
        except ValueError as e:
            raise HubError(400, str(e)) from e
        self.reload_if_changed()
        now = self.clock()
        with self.lock:
            cfg = self.config
            dup = self.db.execute(
                "SELECT id FROM events WHERE dedupe_key=? AND received_at>=? AND status!='duplicate' ORDER BY id DESC LIMIT 1",
                (event["dedupe_key"], now - max(cfg.dedupe_window, _num(raw.get("dedupe_window"), int) or 0)),
            ).fetchone()
            if dup:
                eid = self._insert_event(event, now, "duplicate", f"duplicate of event {dup['id']}")
                return {"event_id": eid, "status": "duplicate", "duplicate_of": dup["id"], "runs": []}

            rules = cfg.match(event)
            if not rules:
                eid = self._insert_event(event, now, "unmatched")
                return {"event_id": eid, "status": "unmatched", "runs": []}

            eid = self._insert_event(event, now, "routed")
            ctx = {**event, "rule": ""}
            run_ids: list[int] = []
            coalesced: list[int] = []
            for rule in rules:
                ctx["rule"] = rule.name
                group_key = render(rule.group, ctx)
                existing = None
                if rule.coalesce:
                    existing = self.db.execute(
                        "SELECT id FROM runs WHERE rule=? AND group_key=? AND status='queued' ORDER BY id LIMIT 1",
                        (rule.name, group_key),
                    ).fetchone()
                if existing:
                    run_id = existing["id"]
                    coalesced.append(run_id)
                else:
                    spec = rule.to_spec()
                    spec["memory"] = cfg.memory
                    spec["require_decision_for"] = cfg.require_decision_for
                    cur = self.db.execute(
                        "INSERT INTO runs (rule, worker_pattern, group_key, concurrency, spec, status, created_at, parent_run) "
                        "VALUES (?,?,?,?,?, 'queued', ?, ?)",
                        (rule.name, rule.worker, group_key, rule.concurrency, json.dumps(spec, ensure_ascii=False), now,
                         parent_run),
                    )
                    run_id = cur.lastrowid
                    run_ids.append(run_id)
                self.db.execute("INSERT OR IGNORE INTO run_events (run_id, event_id) VALUES (?,?)", (run_id, eid))
            if coalesced and not run_ids:
                self.db.execute("UPDATE events SET status='coalesced', note=? WHERE id=?",
                                (f"joined queued run(s) {coalesced}", eid))
            self.cv.notify_all()
            return {
                "event_id": eid,
                "status": "coalesced" if coalesced and not run_ids else "routed",
                "runs": run_ids,
                "coalesced_into": coalesced,
            }

    def _insert_event(self, e: dict[str, Any], now: float, status: str, note: str = "") -> int:
        cur = self.db.execute(
            "INSERT INTO events (received_at, source, type, subject, summary, link, payload, dedupe_key, status, note) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (now, e["source"], e["type"], e["subject"], e["summary"], e["link"],
             json.dumps(e["payload"], ensure_ascii=False), e["dedupe_key"], status, note),
        )
        return cur.lastrowid

    # ------------------------------------------------------------------ claim

    def claim(self, worker: str, wait: float = 0.0, runners: list[str] | None = None) -> dict[str, Any] | None:
        if not worker:
            raise HubError(400, "worker name is required")
        deadline = time.monotonic() + max(0.0, wait)
        with self.cv:
            while True:
                task = self._try_claim(worker, runners)
                if task is not None:
                    return task
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.cv.wait(timeout=min(remaining, 5.0))

    def _try_claim(self, worker: str, runners: list[str] | None) -> dict[str, Any] | None:
        running = self.db.execute("SELECT rule, group_key FROM runs WHERE status='running'").fetchall()
        busy_groups = {r["group_key"] for r in running}
        per_rule: dict[str, int] = {}
        for r in running:
            per_rule[r["rule"]] = per_rule.get(r["rule"], 0) + 1
        for row in self.db.execute("SELECT * FROM runs WHERE status='queued' ORDER BY id").fetchall():
            if not fnmatch.fnmatchcase(worker, row["worker_pattern"]):
                continue
            if row["group_key"] in busy_groups:
                continue
            if per_rule.get(row["rule"], 0) >= row["concurrency"]:
                continue
            spec = json.loads(row["spec"])
            if runners is not None and spec["runner"] not in runners:
                continue
            now = self.clock()
            events = self._events_for_run(row["id"])
            prompt = build_prompt(row["id"], spec, events, row["parent_run"],
                                  attempt=row["attempt"], retry_of=row["retry_of"])
            self.db.execute(
                "UPDATE runs SET status='running', claimed_by=?, claimed_at=?, heartbeat_at=?, prompt=? WHERE id=?",
                (worker, now, now, prompt, row["id"]),
            )
            task = {"run_id": row["id"], **spec, "prompt": prompt, "events": events}
            if row["parent_run"]:
                parent = self.db.execute("SELECT session_id FROM runs WHERE id=?", (row["parent_run"],)).fetchone()
                task["resume_session"] = parent["session_id"] if parent else None
            return task
        return None

    def _events_for_run(self, run_id: int) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT e.* FROM events e JOIN run_events re ON re.event_id=e.id WHERE re.run_id=? ORDER BY e.id",
            (run_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = store.row_to_dict(r)
            out.append({k: d[k] for k in ("id", "source", "type", "subject", "summary", "link", "payload")})
        return out

    # ------------------------------------------------------------------ progress

    def _running(self, run_id: int, worker: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise HubError(404, f"run {run_id} not found")
        if row["status"] != "running" or row["claimed_by"] != worker:
            raise HubError(409, f"run {run_id} is not running on {worker} (status {row['status']})")
        return row

    def heartbeat(self, run_id: int, worker: str) -> None:
        with self.lock:
            self._running(run_id, worker)
            self.db.execute("UPDATE runs SET heartbeat_at=? WHERE id=?", (self.clock(), run_id))

    def finish(self, run_id: int, worker: str, result: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self._running(run_id, worker)
            exit_code = int(result.get("exit_code", 1))
            output = str(result.get("output", ""))
            summary = result.get("summary") or extract_summary(output)
            decision_req = result.get("needs_decision") or extract_decision(output)
            if decision_req is not None and not _valid_decision(decision_req):
                decision_req = None
            reported = _normalize_status(result.get("status")) or extract_status(output)
            if decision_req:
                status = "awaiting_decision"
            elif exit_code != 0 or reported == "failed":
                # The agent's own verdict wins over a clean exit: a process can exit 0 without doing the job.
                status = "failed"
            else:
                status = "succeeded"
            self.db.execute(
                "UPDATE runs SET status=?, finished_at=?, exit_code=?, summary=?, session_id=?, output_tail=?, "
                "decision_request=?, cost_usd=?, input_tokens=?, output_tokens=? WHERE id=?",
                (status, self.clock(), exit_code, (summary or "")[:2000], result.get("session_id"),
                 output[-8000:], json.dumps(decision_req, ensure_ascii=False) if decision_req else None,
                 _num(result.get("cost_usd"), float), _num(result.get("input_tokens"), int),
                 _num(result.get("output_tokens"), int), run_id),
            )
            self.cv.notify_all()
            run = self.get_run(run_id)
        self._maybe_notify(run)
        return {"run_id": run_id, "status": status}

    def decide(self, run_id: int, choice: str, note: str = "", by: str = "human") -> dict[str, Any]:
        with self.lock:
            run = self.get_run(run_id)
            if run is None:
                raise HubError(404, f"run {run_id} not found")
            if run["status"] != "awaiting_decision":
                raise HubError(409, f"run {run_id} is not awaiting a decision (status {run['status']})")
            req = run["decision_request"] or {}
            options = req.get("options") or []
            if options and choice not in options:
                raise HubError(400, f"choice must be one of {options}")
            decision = {"choice": choice, "note": note, "by": by, "at": self.clock()}
            self.db.execute("UPDATE runs SET status='decided', decision=? WHERE id=?",
                            (json.dumps(decision, ensure_ascii=False), run_id))
        # A decision is just another event, so rules decide what happens next.
        follow = self.ingest({
            "source": "dispatchbox",
            "type": "decision.made",
            "subject": f"run-{run_id}",
            "summary": f"{req.get('question', '')} -> {choice}",
            "link": f"{self.public_url}/runs/{run_id}" if self.public_url else "",
            "payload": {"run_id": run_id, "rule": run["rule"], "question": req.get("question"),
                        "choice": choice, "note": note, "session_id": run["session_id"]},
            "dedupe_key": f"dispatchbox:decision:{run_id}",
        }, parent_run=run_id)
        return {"run_id": run_id, "status": "decided", "follow_up": follow}

    # ------------------------------------------------------------------ housekeeping

    def reap(self) -> list[int]:
        """Mark runs whose worker stopped sending heartbeats as lost."""
        now = self.clock()
        lost: list[int] = []
        with self.lock:
            rows = self.db.execute(
                "SELECT id, heartbeat_at, claimed_at, spec, attempt FROM runs WHERE status='running'").fetchall()
            final: list[int] = []
            for r in rows:
                spec = json.loads(r["spec"])
                silent = now - (r["heartbeat_at"] or 0) > self.lost_after
                overdue = now - (r["claimed_at"] or now) > spec.get("timeout", 1800) + self.lost_after
                if not (silent or overdue):
                    continue
                reason = "worker stopped reporting" if silent else "exceeded timeout"
                if r["attempt"] <= spec.get("retries", 0):
                    cur = self.db.execute(
                        "INSERT INTO runs (rule, worker_pattern, group_key, concurrency, spec, status, created_at, "
                        "parent_run, attempt, retry_of) SELECT rule, worker_pattern, group_key, concurrency, spec, "
                        "'queued', ?, parent_run, attempt + 1, id FROM runs WHERE id=?",
                        (now, r["id"]),
                    )
                    self.db.execute("INSERT INTO run_events (run_id, event_id) SELECT ?, event_id FROM run_events "
                                    "WHERE run_id=?", (cur.lastrowid, r["id"]))
                    reason += f", retrying as run #{cur.lastrowid}"
                else:
                    final.append(r["id"])
                self.db.execute("UPDATE runs SET status='lost', finished_at=?, summary=? WHERE id=?",
                                (now, reason, r["id"]))
                lost.append(r["id"])
            if lost:
                self.cv.notify_all()
        # Only tell the owner once nothing will retry.
        for rid in final:
            self._maybe_notify(self.get_run(rid))
        return lost

    def tick_cron(self, now: datetime | None = None) -> list[dict[str, Any]]:
        now = (now or datetime.now()).replace(second=0, microsecond=0)
        stamp = now.strftime("%Y-%m-%dT%H:%M")
        if stamp == self._last_cron_minute:
            return []
        self._last_cron_minute = stamp
        self.reload_if_changed()
        results = []
        for sched in self.config.schedules:
            if sched.cron.matches(now):
                ev = {**sched.event, "dedupe_key": f"cron:{sched.name}:{stamp}"}
                ev.setdefault("summary", f"scheduled {sched.name} at {stamp}")
                results.append(self.ingest(ev))
        return results

    # ------------------------------------------------------------------ notify

    def _maybe_notify(self, run: dict[str, Any] | None) -> None:
        if not run:
            return
        mode = run["spec"].get("notify", "on_decision")
        status = run["status"]
        wanted = (
            mode == "always"
            or (mode in {"on_decision", "on_failure"} and status == "awaiting_decision")
            or (mode == "on_failure" and status in {"failed", "lost"})
        )
        link = self.run_link(run["id"])
        if wanted:
            try:
                self.notifier(run, link)
            except Exception as e:  # notifications must never break the hub
                log.error("notify failed for run %s: %s", run["id"], e)
        # Chat adapters do network calls, so they run off the request thread.
        for adapter in self.adapters.values():
            if wanted:
                self.background(adapter.notify, run, link)
            self.background(adapter.run_finished, run, link)

    def run_link(self, run_id: int) -> str:
        return f"{self.public_url}/runs/{run_id}" if self.public_url else f"/runs/{run_id}"

    def _default_notify(self, run: dict[str, Any], link: str) -> None:
        text = format_notification(run, link)
        url = os.environ.get("DISPATCHBOX_NOTIFY_URL")
        if not url:
            log.warning("NOTIFY %s", text.replace("\n", " | "))
            return
        if "open.feishu.cn" in url or "open.larksuite.com" in url:
            body = {"msg_type": "text", "content": {"text": text}}
        elif "hooks.slack.com" in url:
            body = {"text": text}
        else:
            body = {"text": text, "run": run}
        req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False, default=str).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10, context=ssl_context()).read()

    # ------------------------------------------------------------------ queries

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        with self.lock:
            run = store.row_to_dict(self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            if run:
                run["events"] = self._events_for_run(run_id)
            return run

    def list_runs(self, limit: int = 100, status: str | None = None) -> list[dict[str, Any]]:
        with self.lock:
            if status:
                rows = self.db.execute("SELECT * FROM runs WHERE status=? ORDER BY id DESC LIMIT ?", (status, limit))
            else:
                rows = self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
            return [store.row_to_dict(r) for r in rows.fetchall()]

    def list_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [store.row_to_dict(r) for r in rows]

    def stats(self) -> dict[str, int]:
        with self.lock:
            rows = self.db.execute("SELECT status, COUNT(*) n FROM runs GROUP BY status").fetchall()
            return {r["status"]: r["n"] for r in rows}


def _background(fn: Callable[..., Any], *args: Any) -> None:
    def run() -> None:
        try:
            fn(*args)
        except Exception:
            log.exception("background task %s failed", getattr(fn, "__qualname__", fn))
    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------------- prompt & result contract

def build_prompt(run_id: int, spec: dict[str, Any], events: list[dict[str, Any]], parent_run: int | None,
                 attempt: int = 1, retry_of: int | None = None) -> str:
    latest = events[-1] if events else {}
    ctx = {**latest, "rule": spec["rule"], "event_count": len(events)}
    parts = [render(spec.get("prompt") or "", ctx).strip()]
    if spec.get("skill"):
        parts.append(f"Use the `{spec['skill']}` skill for this task.")

    lines = [f"## Trigger", f"Started automatically by dispatchbox: run #{run_id}, rule \"{spec['rule']}\"."]
    if parent_run:
        lines.append(f"This continues run #{parent_run} after a human decision (see the decision.made event).")
    if retry_of:
        lines.append(f"This is attempt {attempt}. The previous attempt (run #{retry_of}) stopped reporting midway "
                     "and may have done part of the work. Check the current state before repeating any step.")
    if len(events) > 1:
        lines.append(f"{len(events)} related events were merged into this run; handle them together.")
    for e in events:
        line = f"- [{e['source']}/{e['type']}] {e['subject']}: {e['summary']}".rstrip(": ")
        if e.get("link"):
            line += f" ({e['link']})"
        lines.append(line)
    payloads = [{"event": e["id"], "payload": e["payload"]} for e in events if e.get("payload")]
    if payloads:
        blob = json.dumps(payloads, ensure_ascii=False, indent=1)
        if len(blob) > MAX_PAYLOAD_IN_PROMPT:
            blob = blob[:MAX_PAYLOAD_IN_PROMPT] + "\n... (truncated)"
        lines += ["", "Event payloads:", "```json", blob, "```"]
    parts.append("\n".join(lines))

    if spec.get("memory"):
        files = ", ".join(f"`{m}`" for m in spec["memory"])
        parts.append(
            "## Standing decisions\n"
            f"Before asking anything, read {files} if they exist. They record what the owner already decided "
            "or refused. Follow them and do not ask again about anything they settle."
        )

    contract = ["## When to ask a human"]
    if spec.get("require_decision_for"):
        contract.append("Stop and ask only before these kinds of actions: "
                        + ", ".join(spec["require_decision_for"]) + ". Do everything else yourself.")
    else:
        contract.append("Only ask when you genuinely cannot proceed safely. Do everything else yourself.")
    contract += [
        "To ask, do not take the action. End your final message with exactly one line:",
        f'{DECISION_MARK} {{"question": "<one sentence>", "options": ["<option A>", "<option B>"], "context": "<what you found>"}}',
        "",
        "Always finish with a summary line. Unless you are asking, put a status line right before it:",
        "failed if you could not do what was asked, for any reason, including a denied permission or a",
        "missing tool; succeeded only if the task is actually done. Asking is not a failure, so when you",
        "ask, leave the status line out.",
        f"{STATUS_MARK} succeeded | failed",
        f"{SUMMARY_MARK} <one sentence>",
    ]
    parts.append("\n".join(contract))
    return "\n\n".join(p for p in parts if p)


def extract_summary(output: str) -> str:
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith(SUMMARY_MARK):
            return line[len(SUMMARY_MARK):].strip()
    tail = [l.strip() for l in output.strip().splitlines()
            if l.strip() and not l.strip().startswith((DECISION_MARK, STATUS_MARK))]
    return tail[-1][:500] if tail else ""


def _normalize_status(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    v = value.strip().strip("`*").lower()
    if v in {"succeeded", "success", "done", "ok", "completed"}:
        return "succeeded"
    if v in {"failed", "failure", "error", "blocked", "incomplete"}:
        return "failed"
    return None


def extract_status(output: str) -> str | None:
    """Read the agent's own verdict from its last DISPATCHBOX_STATUS line, if any."""
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith(STATUS_MARK):
            return _normalize_status(line[len(STATUS_MARK):])
    return None


def _num(value: Any, kind: type) -> Any:
    try:
        return kind(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def extract_decision(output: str) -> dict[str, Any] | None:
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith(DECISION_MARK):
            try:
                return json.loads(line[len(DECISION_MARK):].strip())
            except json.JSONDecodeError:
                return {"question": line[len(DECISION_MARK):].strip(), "options": []}
    return None


def _valid_decision(d: Any) -> bool:
    return isinstance(d, dict) and isinstance(d.get("question"), str) and d["question"].strip() != "" \
        and isinstance(d.get("options", []), list)


def format_notification(run: dict[str, Any], link: str) -> str:
    status = run["status"]
    head = f"[dispatchbox] run #{run['id']} {run['rule']}: {status}"
    lines = [head]
    if status == "awaiting_decision" and run.get("decision_request"):
        req = run["decision_request"]
        lines.append(f"Question: {req.get('question')}")
        if req.get("options"):
            lines.append("Options: " + " / ".join(req["options"]))
        if req.get("context"):
            lines.append(f"Context: {req['context']}")
    elif run.get("summary"):
        lines.append(run["summary"])
    lines.append(f"Open: {link}")
    return "\n".join(lines)
