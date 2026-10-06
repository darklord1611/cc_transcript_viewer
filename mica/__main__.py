"""Command line for Mica.

    python -m mica daemon  [--config FILE | --store DIR [--home DIR] [--source NAME:PARSER:ROOT ...]]
    python -m mica status  [--store DIR] [--json]
    python -m mica flagged [--store DIR]
    python -m mica verify  [--store DIR]
    sudo /usr/bin/python3 -m mica install   [--user NAME] [--yes] [--dry-run]
    sudo /usr/bin/python3 -m mica uninstall [--delete-store] [--yes] [--dry-run]

``daemon`` without --config is a development mode: it runs as *you*, so it
offers no protection from agents, but it exercises exactly the capture code
the installed daemon runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from mica import capture
from mica import store as v


def _load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        config = json.load(fh)
    if not isinstance(config, dict) or not config.get("mica") or not config.get("sources"):
        raise SystemExit(f"{path}: config needs 'mica' and 'sources'")
    return config


def _parse_source(text: str) -> capture.Source:
    try:
        name, parser, root = text.split(":", 2)
    except ValueError:
        raise SystemExit(f"--source must be NAME:PARSER:ROOT, got {text!r}")
    return capture.Source(name, parser, root)


def cmd_daemon(args) -> int:
    if args.config:
        config = _load_config(Path(args.config))
        store_root = config["mica"]
        sources = [capture.Source.from_json(s) for s in config["sources"]]
        poll = float(config.get("poll_seconds") or capture.DEFAULT_POLL_SECONDS)
    else:
        if not args.store:
            raise SystemExit("daemon needs --config or --store")
        store_root = args.store
        sources = [_parse_source(s) for s in args.source] if args.source else capture.default_sources(args.home)
        poll = args.poll
    capturer = capture.Capturer(
        store_root,
        sources,
        poll_seconds=poll,
        log=lambda msg: print(msg, flush=True),
    )
    print(f"mica: capturing into {store_root}", flush=True)
    for src in sources:
        print(f"  {src.name:<16} {src.root}", flush=True)
    try:
        capturer.run_forever()
    except KeyboardInterrupt:
        return 0
    return 0


def _reader(args) -> v.StoreReader:
    reader = v.StoreReader(args.store)
    if not reader.available():
        raise SystemExit(f"no Mica store at {args.store}")
    return reader


def cmd_status(args) -> int:
    reader = _reader(args)
    status = reader.status()
    if args.json:
        print(json.dumps(status, indent=2))
        return 0
    age = status["heartbeat_age"]
    state = "running" if status["running"] else "NOT RUNNING"
    print(f"mica:    {status['root']}")
    print(f"daemon:   {state}" + (f" (last heartbeat {age:.0f}s ago)" if age is not None else ""))
    print(f"files:    {status['n_files']} captured, {status['n_flagged']} flagged")
    for name, src in sorted(status["sources"].items()):
        note = "ok" if src.get("ok") else f"PROBLEM: {src.get('error')}"
        if src.get("ok") and src.get("error") == "missing":
            note = "not present"
        print(f"  {name:<16} {src.get('root')}  [{note}]")
    return 0 if status["running"] else 1


def cmd_flagged(args) -> int:
    reader = _reader(args)
    flagged = [(k, e) for k, e in sorted(reader.index().items()) if e.get("flags")]
    if not flagged:
        print("no flagged transcripts")
        return 0
    for key, entry in flagged:
        print(f"{key}  [{', '.join(entry['flags'])}]  {entry.get('path')}")
        for event in reader.events(key):
            if event.get("type") in v.TAMPER_EVENTS:
                print(f"    {event.get('t')}  {event.get('type')}  {json.dumps(event.get('detail') or {})}")
    return 0


def cmd_verify(args) -> int:
    problems = capture.verify_store(args.store)
    if problems:
        for problem in problems:
            print(problem)
        return 1
    print("all generation hash chains verify")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mica", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("daemon", help="run the capture loop")
    d.add_argument("--config", help="installed config.json")
    d.add_argument("--store", help="store directory (development mode)")
    d.add_argument("--home", default=str(Path.home()), help="home whose transcripts to capture")
    d.add_argument("--source", action="append", help="NAME:PARSER:ROOT (repeatable; default: Claude + Codex)")
    d.add_argument("--poll", type=float, default=capture.DEFAULT_POLL_SECONDS)
    d.set_defaults(func=cmd_daemon)

    for name, func, text in (
        ("status", cmd_status, "is the daemon running, and what does it cover"),
        ("flagged", cmd_flagged, "list transcripts with tamper events"),
        ("verify", cmd_verify, "recheck Mica's own hash chains"),
    ):
        p = sub.add_parser(name, help=text)
        p.add_argument("--store", default=str(v.DEFAULT_STORE_DIR))
        if name == "status":
            p.add_argument("--json", action="store_true")
        p.set_defaults(func=func)

    from mica import install

    i = sub.add_parser("install", help="install the daemon (macOS, needs sudo)")
    install.add_install_args(i)
    i.set_defaults(func=install.cmd_install)
    u = sub.add_parser("uninstall", help="remove the daemon (macOS, needs sudo)")
    install.add_uninstall_args(u)
    u.set_defaults(func=install.cmd_uninstall)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
