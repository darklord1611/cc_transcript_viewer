#!/usr/bin/env python3
"""Aggregate transcripts mirrored from many remote pods into one viewer.

Phase 1 of the multi-pod "auditing game" oversight setup (red/blue/green agents,
each in its own RunPod pod). A separate collector (``collector.py``) rsyncs each
pod's transcript directories into a local mirror on the hub:

    <mirror>/<pod-id>/
        pod.json                     # {"team": "...", "game": "...", "label": "..."}
        claude/projects/...          # rsync of the pod's ~/.claude/projects
        codex/sessions/...           # rsync of the pod's ~/.codex/sessions
        codex/archived_sessions/...  # rsync of ~/.codex/archived_sessions (optional)

This module points the *existing* Claude Code / Codex parsers at each pod's roots
in turn and tags every session with its ``pod``/``team``/``game``. Everything else
in the server — search, live refresh, rendering, and the mtime summary cache —
then works unchanged, because the cache is keyed by absolute path and pod dirs
never collide. All multi-pod logic is deliberately confined here so local
(single-machine) mode stays exactly as it was.

Two things this module is careful about:

* **The summary caches are never cleared.** The parsers' ``configure()`` clears
  their cache, which would defeat the mtime cache on every poll. We set the
  parser module globals directly instead, so cached summaries survive across
  pods and across polls.
* **Global swaps are serialized.** The parsers keep a single mutable module-global
  root; the server is threaded and the ~1s live poller calls in constantly. A
  reentrant lock guards every section that mutates a parser global, so a scan for
  pod A can't observe pod B's root mid-flight.

No network code lives here. The mirror is populated out-of-band by the collector;
this module only reads local files, exactly like the parsers it drives — so the
server's "no outbound connections" guarantee is untouched.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

import claude_parser as claude
import codex_parser as codex

MIRROR_DIR: Path | None = None

# Serializes every parser-global swap below; see the module docstring.
_LOCK = threading.RLock()


def configure(mirror_dir) -> None:
    """Enable mirror mode against ``mirror_dir``; pass None to disable it."""
    global MIRROR_DIR
    MIRROR_DIR = Path(mirror_dir).expanduser().resolve() if mirror_dir else None


def enabled() -> bool:
    return MIRROR_DIR is not None


def _under(target: Path, root: Path) -> bool:
    """True if ``target`` is ``root`` or lives beneath it (a local copy of the
    server's own check, duplicated to avoid importing server.py circularly)."""
    try:
        root = root.resolve()
    except OSError:
        return False
    return target == root or root in target.parents


def _pods() -> list[dict]:
    """Discover pod subdirectories of the mirror and their metadata.

    Every immediate subdirectory of the mirror is a pod. ``pod.json`` is
    optional — a pod with none still lists, tagged only by its directory name.
    """
    if MIRROR_DIR is None or not MIRROR_DIR.exists():
        return []
    pods: list[dict] = []
    for pod_dir in sorted(MIRROR_DIR.iterdir()):
        if not pod_dir.is_dir():
            continue
        meta: dict = {}
        meta_file = pod_dir / "pod.json"
        if meta_file.exists():
            try:
                loaded = json.loads(meta_file.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    meta = loaded
            except (OSError, json.JSONDecodeError):
                meta = {}

        def _str(key: str) -> str:
            value = meta.get(key)
            return str(value) if value not in (None, "") else ""

        pods.append({
            "id": _str("pod") or pod_dir.name,
            "team": _str("team"),
            "game": _str("game"),
            "label": _str("label"),
            "claude_root": pod_dir / "claude" / "projects",
            "codex_home": pod_dir / "codex",
        })
    return pods


def _tag(session: dict, pod: dict) -> dict:
    """Stamp a summary/session dict with its originating pod's identity."""
    session["pod"] = pod["id"]
    if pod["team"]:
        session["team"] = pod["team"]
    if pod["game"]:
        session["game"] = pod["game"]
    if pod["label"]:
        session["pod_label"] = pod["label"]
    return session


def _point_codex_at(codex_home: Path) -> None:
    codex.CODEX_HOME = codex_home
    codex.SESSIONS_DIR = codex_home / "sessions"
    codex.ARCHIVED_SESSIONS_DIR = codex_home / "archived_sessions"
    codex.STATE_DB = codex_home / "state_5.sqlite"


def collect() -> list[dict]:
    """Session summaries across every pod, each tagged with pod/team/game.

    Mirrors the shape of the local ``claude.list_sessions() + codex.list_sessions()``
    concatenation the server does in single-machine mode, so the caller's
    downstream handling (custom names, sort, sub-agent grouping) is identical.
    """
    out: list[dict] = []
    with _LOCK:
        for pod in _pods():
            if pod["claude_root"].exists():
                claude.PROJECTS_DIR = pod["claude_root"]
                try:
                    for s in claude.list_sessions():
                        out.append(_tag(s, pod))
                except Exception:  # noqa: BLE001 — one bad pod must not hide the rest
                    pass
            if (pod["codex_home"] / "sessions").exists():
                _point_codex_at(pod["codex_home"])
                try:
                    for s in codex.list_sessions():
                        out.append(_tag(s, pod))
                except Exception:  # noqa: BLE001
                    pass
    return out


def _owning_pod(target: Path):
    """Return (pod, agent) for the pod root that contains ``target``, else (None, None)."""
    for pod in _pods():
        if _under(target, pod["claude_root"]):
            return pod, "claude"
        codex_home = pod["codex_home"]
        if _under(target, codex_home / "sessions") or _under(
            target, codex_home / "archived_sessions"
        ):
            return pod, "codex"
    return None, None


def owns(target: Path) -> bool:
    """True if ``target`` resolves inside some pod's transcript roots.

    This is the allowed-root check for mirror mode: it confines ``/api/session``
    reads to the mirrored pod trees, same role the server's root list plays
    locally. No global is mutated, so no lock is needed.
    """
    pod, _agent = _owning_pod(target)
    return pod is not None


def parse(target: Path) -> dict | None:
    """Parse a pod transcript, pointing the right parser at its pod first."""
    with _LOCK:
        pod, agent = _owning_pod(target)
        if pod is None:
            return None
        if agent == "claude":
            claude.PROJECTS_DIR = pod["claude_root"]
            data = claude.parse_session(target)
        else:
            _point_codex_at(pod["codex_home"])
            data = codex.parse_session(target)
        if data is not None:
            _tag(data, pod)
        return data
