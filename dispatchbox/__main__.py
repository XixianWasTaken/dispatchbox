"""Command line entry point: `python -m dispatchbox hub|worker|send|check`."""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import urllib.error
import urllib.request

from . import __version__
from .net import context_for


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dispatchbox", description="Route events to coding agents on your own machines.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    h = sub.add_parser("hub", help="run the hub (events in, tasks out)")
    h.add_argument("--rules", default="rules.json")
    h.add_argument("--db", default="dispatchbox.db")
    h.add_argument("--host", default="127.0.0.1")
    h.add_argument("--port", type=int, default=8787)
    h.add_argument("--public-url", default=os.environ.get("DISPATCHBOX_PUBLIC_URL", ""),
                   help="URL people use to reach the hub, used in notification links")

    w = sub.add_parser("worker", help="run a worker that pulls tasks and runs agents")
    w.add_argument("--hub", default=os.environ.get("DISPATCHBOX_HUB", "http://127.0.0.1:8787"))
    w.add_argument("--name", default=socket.gethostname().split(".")[0])
    w.add_argument("--slots", type=int, default=2, help="how many runs at once")
    w.add_argument("--allow-dir", action="append", dest="allow_dirs",
                   help="directory runs may use (repeatable); defaults to the current directory")
    w.add_argument("--runners", default="claude,codex", help="comma list; add 'shell' to allow shell rules")
    w.add_argument("--claude-bin", default="claude")
    w.add_argument("--codex-bin", default="codex")

    s = sub.add_parser("send", help="send one event to the hub")
    s.add_argument("source")
    s.add_argument("type")
    s.add_argument("--subject", default="")
    s.add_argument("--summary", default="")
    s.add_argument("--link", default="")
    s.add_argument("--payload", default="{}", help="JSON object")
    s.add_argument("--hub", default=os.environ.get("DISPATCHBOX_HUB", "http://127.0.0.1:8787"))

    c = sub.add_parser("check", help="validate a rules file and show which rule an event would hit")
    c.add_argument("--rules", default="rules.json")
    c.add_argument("--source")
    c.add_argument("--type")
    c.add_argument("--subject", default="")

    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    token = os.environ.get("DISPATCHBOX_TOKEN", "")

    if args.cmd == "hub":
        from .hub import Hub
        from .server import serve
        from .chat import ChatError, adapters_from_env
        hub = Hub(args.rules, args.db, public_url=args.public_url)
        try:
            hub.adapters = adapters_from_env(hub, dict(os.environ))
        except ChatError as e:
            print(f"Cannot start: {e}", file=sys.stderr)
            return 2
        srv = serve(hub, args.host, args.port, token, os.environ.get("DISPATCHBOX_GITHUB_SECRET", ""))
        logging.getLogger("dispatchbox").info("hub listening on http://%s:%d with %d rules, %d schedules, chat: %s",
                                        args.host, args.port, len(hub.config.rules), len(hub.config.schedules),
                                        ", ".join(hub.adapters) or "none")
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        return 0

    if args.cmd == "worker":
        from .worker import WorkerConfig, serve_worker
        if not token:
            print("Set DISPATCHBOX_TOKEN to the hub's token.", file=sys.stderr)
            return 2
        cfg = WorkerConfig(hub=args.hub, name=args.name, token=token, slots=args.slots,
                           allow_dirs=args.allow_dirs or [os.getcwd()],
                           runners=[r.strip() for r in args.runners.split(",") if r.strip()],
                           claude_bin=args.claude_bin, codex_bin=args.codex_bin)
        try:
            serve_worker(cfg)
        except KeyboardInterrupt:
            pass
        return 0

    if args.cmd == "send":
        body = {"source": args.source, "type": args.type, "subject": args.subject, "summary": args.summary,
                "link": args.link, "payload": json.loads(args.payload)}
        req = urllib.request.Request(args.hub.rstrip("/") + "/events", data=json.dumps(body).encode(), method="POST",
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15, context=context_for(req.full_url)) as resp:
                print(resp.read().decode())
        except urllib.error.HTTPError as e:
            print(f"{e.code}: {e.read().decode()}", file=sys.stderr)
            return 1
        return 0

    if args.cmd == "check":
        from .rules import load_config_file, normalize_event
        try:
            cfg = load_config_file(args.rules)
        except Exception as e:
            print(f"invalid: {e}", file=sys.stderr)
            return 1
        print(f"ok: {len(cfg.rules)} rules, {len(cfg.schedules)} schedules, dedupe window {cfg.dedupe_window}s")
        if args.source and args.type:
            ev = normalize_event({"source": args.source, "type": args.type, "subject": args.subject})
            hits = cfg.match(ev)
            print("matches: " + (", ".join(r.name for r in hits) if hits else "(none)"))
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
