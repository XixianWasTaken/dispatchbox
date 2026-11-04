"""Worker: runs on a machine that has your agents installed, pulls tasks from the hub and runs them.

The worker only makes outbound requests, so the machine never needs to be reachable from the internet.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .net import context_for

log = logging.getLogger("dispatchbox.worker")

HEARTBEAT_EVERY = 20
OUTPUT_LIMIT = 200_000


class HubGone(Exception):
    """The hub no longer considers this run ours (it was reaped or finished elsewhere)."""


@dataclass
class WorkerConfig:
    hub: str
    name: str
    token: str
    slots: int = 2
    wait: int = 50
    allow_dirs: list[str] = field(default_factory=lambda: [os.getcwd()])
    runners: list[str] = field(default_factory=lambda: ["claude", "codex"])
    claude_bin: str = "claude"
    codex_bin: str = "codex"


class HubClient:
    def __init__(self, base: str, token: str):
        self.base = base.rstrip("/")
        self.token = token

    def _req(self, method: str, path: str, body: Any = None, timeout: float = 30) -> tuple[int, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=context_for(req.full_url)) as resp:
                raw = resp.read()
                return resp.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                payload = raw.decode(errors="replace")
            return e.code, payload

    def claim(self, worker: str, wait: int, runners: list[str]) -> dict[str, Any] | None:
        q = urllib.parse.urlencode({"worker": worker, "wait": wait, "runners": ",".join(runners)})
        status, body = self._req("GET", f"/claim?{q}", timeout=wait + 15)
        if status == 204:
            return None
        if status != 200:
            raise RuntimeError(f"claim failed: {status} {body}")
        return body

    def heartbeat(self, run_id: int, worker: str) -> None:
        status, body = self._req("POST", f"/runs/{run_id}/heartbeat", {"worker": worker})
        if status == 409 or status == 404:
            raise HubGone(str(body))
        if status != 200:
            raise RuntimeError(f"heartbeat failed: {status} {body}")

    def result(self, run_id: int, worker: str, result: dict[str, Any]) -> None:
        delay = 2.0
        deadline = time.monotonic() + 600
        while True:
            try:
                status, body = self._req("POST", f"/runs/{run_id}/result", {"worker": worker, **result})
                if status == 200:
                    return
                if status in (404, 409):
                    log.warning("hub refused result for run %s: %s", run_id, body)
                    return
                err = f"{status} {body}"
            except (urllib.error.URLError, OSError) as e:
                err = str(e)
            if time.monotonic() > deadline:
                log.error("giving up reporting run %s: %s", run_id, err)
                return
            log.warning("reporting run %s failed (%s), retrying in %.0fs", run_id, err, delay)
            time.sleep(delay)
            delay = min(delay * 2, 60)


def resolve_cwd(raw: str, allow_dirs: list[str]) -> Path:
    path = Path(os.path.expanduser(raw or ".")).resolve()
    allowed = [Path(os.path.expanduser(d)).resolve() for d in allow_dirs]
    if not any(path == a or a in path.parents for a in allowed):
        raise PermissionError(f"{path} is outside the worker's allowed directories")
    if not path.is_dir():
        raise FileNotFoundError(f"{path} does not exist")
    return path


def build_command(task: dict[str, Any], cfg: WorkerConfig, tmpdir: str) -> tuple[list[str], str | None]:
    """Return (argv, stdin text). Prompts go through stdin so they never show up in `ps`."""
    runner = task["runner"]
    extra = [str(a) for a in task.get("args") or []]
    model = task.get("model")
    resume = task.get("resume_session")
    if runner == "claude":
        cmd = [cfg.claude_bin, "-p", "--output-format", "json"]
        if model:
            cmd += ["--model", model]
        if resume:
            cmd += ["--resume", resume]
        return cmd + extra, task["prompt"]
    if runner == "codex":
        last = os.path.join(tmpdir, "last_message.txt")
        cmd = [cfg.codex_bin, "exec"]
        if resume:
            cmd += ["resume", resume]
            extra = _sandbox_as_config(extra)
        cmd += ["--json", "-o", last]
        if model:
            cmd += ["-m", model]
        return cmd + extra + ["-"], task["prompt"]
    if runner == "shell":
        return ["/bin/sh", "-c", task["command"]], None
    raise ValueError(f"unknown runner {runner!r}")


def _sandbox_as_config(args: list[str]) -> list[str]:
    """`codex exec resume` rejects -s/--sandbox, so pass the same choice as a config override."""
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in {"-s", "--sandbox"} and i + 1 < len(args):
            out += ["-c", f'sandbox_mode="{args[i + 1]}"']
            i += 2
            continue
        if a.startswith("--sandbox="):
            out += ["-c", f'sandbox_mode="{a.split("=", 1)[1]}"']
        else:
            out.append(a)
        i += 1
    return out


def parse_output(runner: str, stdout: str, stderr: str, tmpdir: str) -> dict[str, Any]:
    """Pull the agent's final message and session id out of the raw process output."""
    if runner == "claude":
        try:
            data = json.loads(stdout.strip().splitlines()[-1]) if stdout.strip() else {}
        except (json.JSONDecodeError, IndexError):
            data = {}
        if isinstance(data, list):
            # With verbose on, by flag or in its settings, Claude prints every message as one JSON array on one line.
            data = next((m for m in reversed(data) if isinstance(m, dict) and m.get("type") == "result"), {})
        if isinstance(data, dict) and "result" in data:
            out = {"output": str(data.get("result") or ""), "session_id": data.get("session_id")}
            if data.get("is_error"):
                out["agent_error"] = True
            if data.get("total_cost_usd") is not None:
                out["cost_usd"] = data["total_cost_usd"]
            usage = data.get("usage") or {}
            if isinstance(usage, dict) and usage:
                # Claude reports cache reads and writes separately from fresh input; count all of it as input.
                out["input_tokens"] = sum(int(usage.get(k) or 0) for k in
                                          ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                out["output_tokens"] = int(usage.get("output_tokens") or 0)
            return out
        return {"output": (stdout + ("\n" + stderr if stderr else "")).strip()}
    if runner == "codex":
        session = None
        tokens_in = tokens_out = 0
        saw_usage = False
        errors: list[str] = []
        for line in stdout.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            kind = ev.get("type")
            if kind in {"thread.started", "session.started"}:
                session = ev.get("thread_id") or ev.get("session_id") or session
            elif kind == "turn.completed" and isinstance(ev.get("usage"), dict):
                # Codex's input_tokens already includes cached_input_tokens.
                saw_usage = True
                tokens_in += int(ev["usage"].get("input_tokens") or 0)
                tokens_out += int(ev["usage"].get("output_tokens") or 0)
            elif kind == "turn.failed":
                errors.append(str((ev.get("error") or {}).get("message", "turn failed")))
            elif kind == "error":
                errors.append(str(ev.get("message", "error")))
        last = Path(tmpdir, "last_message.txt")
        text = last.read_text(errors="replace").strip() if last.exists() else ""
        out: dict[str, Any] = {"session_id": session}
        if errors and not text:
            out["output"] = "codex failed: " + errors[-1][:2000]
            out["agent_error"] = True
        else:
            out["output"] = text or stderr.strip()[-4000:]
        if saw_usage:
            out["input_tokens"], out["output_tokens"] = tokens_in, tokens_out
        return out
    return {"output": (stdout + ("\n" + stderr if stderr else "")).strip()}


def run_task(task: dict[str, Any], cfg: WorkerConfig, client: HubClient) -> dict[str, Any]:
    run_id = task["run_id"]
    try:
        cwd = resolve_cwd(task.get("cwd", "."), cfg.allow_dirs)
    except (PermissionError, FileNotFoundError) as e:
        return {"exit_code": 126, "output": f"dispatchbox worker refused to run: {e}", "summary": str(e)}

    with tempfile.TemporaryDirectory(prefix=f"dispatchbox-{run_id}-") as tmpdir:
        events_file = Path(tmpdir, "events.json")
        events_file.write_text(json.dumps(task.get("events", []), ensure_ascii=False, indent=1))
        env = {**os.environ, "DISPATCHBOX_RUN_ID": str(run_id), "DISPATCHBOX_RULE": task["rule"],
               "DISPATCHBOX_EVENTS_FILE": str(events_file), "DISPATCHBOX_HUB": cfg.hub}
        env.pop("DISPATCHBOX_TOKEN", None)  # agents never see the hub token
        try:
            cmd, stdin = build_command(task, cfg, tmpdir)
        except (ValueError, KeyError) as e:
            return {"exit_code": 2, "output": str(e), "summary": str(e)}

        log.info("run #%s %s: %s in %s", run_id, task["rule"], cmd[0], cwd)
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        except FileNotFoundError as e:
            return {"exit_code": 127, "output": str(e), "summary": f"{cmd[0]} is not installed on this worker"}

        stop = threading.Event()
        gone = threading.Event()

        def beat() -> None:
            while not stop.wait(HEARTBEAT_EVERY):
                try:
                    client.heartbeat(run_id, cfg.name)
                except HubGone:
                    log.warning("hub dropped run #%s, stopping it", run_id)
                    gone.set()
                    _kill(proc)
                    return
                except Exception as e:
                    log.warning("heartbeat for run #%s failed: %s", run_id, e)

        threading.Thread(target=beat, daemon=True).start()
        timed_out = False
        try:
            stdout, stderr = proc.communicate(input=stdin, timeout=task.get("timeout", 1800))
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill(proc)
            stdout, stderr = proc.communicate()
        finally:
            stop.set()

        result = parse_output(task["runner"], stdout[-OUTPUT_LIMIT:], stderr[-OUTPUT_LIMIT:], tmpdir)
        code = proc.returncode
        if timed_out:
            code = 124
            result["summary"] = f"timed out after {task.get('timeout')}s"
        elif result.pop("agent_error", False) and code == 0:
            code = 1
        result["exit_code"] = code
        if gone.is_set():
            result["summary"] = "stopped because the hub dropped this run"
        return result


def _kill(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=10)
            return
        except subprocess.TimeoutExpired:
            continue


def serve_worker(cfg: WorkerConfig, stop: threading.Event | None = None) -> None:
    stop = stop or threading.Event()
    client = HubClient(cfg.hub, cfg.token)
    slots = threading.Semaphore(cfg.slots)
    backoff = 2.0
    log.info("worker %s polling %s (slots=%d, runners=%s, dirs=%s)", cfg.name, cfg.hub, cfg.slots,
             ",".join(cfg.runners), ", ".join(cfg.allow_dirs))

    def work(task: dict[str, Any]) -> None:
        try:
            result = run_task(task, cfg, client)
            client.result(task["run_id"], cfg.name, result)
            log.info("run #%s finished with exit %s", task["run_id"], result.get("exit_code"))
        except Exception:
            log.exception("run #%s crashed", task["run_id"])
            client.result(task["run_id"], cfg.name, {"exit_code": 1, "output": "worker crashed", "summary": "worker crashed"})
        finally:
            slots.release()

    while not stop.is_set():
        if not slots.acquire(timeout=1):
            continue
        try:
            task = client.claim(cfg.name, cfg.wait, cfg.runners)
            backoff = 2.0
        except Exception as e:
            slots.release()
            log.warning("cannot reach hub (%s), retrying in %.0fs", e, backoff)
            stop.wait(backoff)
            backoff = min(backoff * 2, 60)
            continue
        if task is None:
            slots.release()
            continue
        threading.Thread(target=work, args=(task,), name=f"run-{task['run_id']}", daemon=True).start()
