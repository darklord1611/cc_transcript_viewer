#!/usr/bin/env python3
"""Run-structured view of the auditing game: one nested tree per run number.

Where mirror.py groups transcripts by *pod*, this groups them by **round** then
**run number** — the unifying key defined by red's ledger and reused by blue and
green (see AutoSandbag/plans/ARTIFACTS_INDEX.md). Each target model is a round
(opus48, sonnet5, …); run numbers repeat across rounds, so the round namespaces
them. The durable transcripts live on each team's /workspace store and are staged
(by collect_runs.py) into:

    runs_mirror/
        <round>/                           (opus48, sonnet5, …)
            run<N>/
                red/    <organism>.jsonl    (or _LOST.jsonl placeholder)
                blue/   audit_runs_<K>/agent_transcript/<uuid>.jsonl
                green/  <slug>/<uuid>.jsonl
                status.txt                 (optional: run outcome, e.g. REFUSED_AUP)

Each run renders as a nested tree: the **red organism** is the parent row, and
its **5 blue audits + green eval** hang beneath it as children — reusing the
viewer's existing sub-agent nesting (is_subagent + parent_file). A run whose red
transcript was lost gets a `_LOST.jsonl` placeholder so the note shows in place.

The transcripts are ordinary claude_code JSONL, so claude_parser reads each file
directly; this module only enumerates, tags (run/team/role), and links them. It
reads local files only — no network, same as the parsers it drives.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

import claude_parser as claude

RUNS_DIR: Path | None = None
_LOCK = threading.RLock()

# Role → the badge/label shown in the sidebar; also the child ordering.
_TEAM_BY_ROLE = {"organism": "red", "audit": "blue", "eval": "green"}

# Round dir name → (display label, sort order). Rounds are the {model}×{domain}
# combos of the red-team archive; newer/sonnet first. Unknown rounds fall back to
# their dir name and sort last.
_ROUND_META = {
    "sonnet5-code": ("sonnet-5 · code", 0),
    "sonnet5-math": ("sonnet-5 · math", 1),
    "opus48-code": ("opus-4.8 · code", 2),
    "opus48-math": ("opus-4.8 · math", 3),
    # legacy single-domain labels (kept for back-compat)
    "sonnet5": ("sonnet-5", 4),
    "opus48": ("opus-4.8", 5),
}


def configure(runs_dir) -> None:
    global RUNS_DIR
    RUNS_DIR = Path(runs_dir).expanduser().resolve() if runs_dir else None


def enabled() -> bool:
    return RUNS_DIR is not None


# Named controls that sit after the numbered runs, in this order.
_CONTROL_ORDER = {"overt": 1, "clean": 2}


def _run_num(name: str) -> int | None:
    m = re.fullmatch(r"run(\d+)", name)
    return int(m.group(1)) if m else None


def _sort_key(name: str) -> tuple[int, int, str]:
    """Numbered runs first (by number), then named controls (overt, clean)."""
    n = _run_num(name)
    if n is not None:
        return (0, n, "")
    return (1, _CONTROL_ORDER.get(name, 99), name)


def _round_dirs() -> list[Path]:
    """Immediate subdirs of the runs dir that hold run<N>/ dirs, newest round first."""
    if RUNS_DIR is None or not RUNS_DIR.exists():
        return []
    rounds = [
        d for d in RUNS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith(".") and _run_dirs(d)
    ]
    return sorted(rounds, key=lambda d: (_ROUND_META.get(d.name, (d.name, 99))[1], d.name))


def _run_dirs(round_dir: Path) -> list[Path]:
    dirs = [
        d for d in round_dir.iterdir()
        if d.is_dir() and (_run_num(d.name) is not None or d.name in _CONTROL_ORDER)
    ]
    return sorted(dirs, key=lambda d: _sort_key(d.name))


def _jsonl(root: Path) -> list[Path]:
    return sorted(root.rglob("*.jsonl")) if root.exists() else []


def _red_path(run_dir: Path) -> Path | None:
    """The organism transcript, preferring a real one over the _LOST placeholder."""
    real = [p for p in _jsonl(run_dir / "red") if p.name != "_LOST.jsonl"]
    if real:
        return real[0]
    placeholder = run_dir / "red" / "_LOST.jsonl"
    return placeholder if placeholder.exists() else None


def _blue_audits(run_dir: Path) -> list[tuple[str, Path]]:
    """(role_label, transcript) for each blue audit_runs_<K>, ordered by K.

    A variant dir like ``audit_runs_1_RESUME`` keeps its suffix in the label
    (``audit 1 · resume``) so it doesn't collide with the plain ``audit 1``.
    """
    out: list[tuple[int, str, Path]] = []
    blue = run_dir / "blue"
    if not blue.exists():
        return []
    for audit_dir in sorted(blue.iterdir()):
        m = re.match(r"audit_runs_(\d+)(.*)$", audit_dir.name)
        if not audit_dir.is_dir() or not m:
            continue
        files = _jsonl(audit_dir)
        if not files:
            continue
        k = int(m.group(1))
        suffix = m.group(2).strip("_").replace("_", " ").lower()
        label = f"audit {k}" + (f" · {suffix}" if suffix else "")
        out.append((k, label, files[0]))
    return [(label, path) for _k, label, path in sorted(out, key=lambda t: (t[0], t[1]))]


def _green_evals(run_dir: Path) -> list[Path]:
    return _jsonl(run_dir / "green")


def _summary(path: Path) -> dict:
    # Copy so tagging never mutates claude_parser's cached summary dict.
    return dict(claude.session_summary(path))


def _tag(s: dict, *, run: str, round_label: str, role: str, parent_file: str | None,
         parent_id: str | None, label: str | None = None) -> dict:
    s["run"] = run
    s["round"] = round_label
    s["team"] = _TEAM_BY_ROLE[role]
    s["role"] = label or role
    if parent_file is not None:  # a child (blue/green) nested under the red parent
        s["is_subagent"] = True
        s["parent_file"] = parent_file
        s["parent_id"] = parent_id
        s["subagent_type"] = f"{s['team']}-{role}"
        s["subagent_description"] = s["role"]
    else:
        s["is_subagent"] = False
    return s


def collect() -> list[dict]:
    """Sessions for every run, emitted parent-then-children so the sidebar nests
    them directly (server.list_sessions keeps this order in run mode)."""
    out: list[dict] = []
    with _LOCK:
        for round_dir in _round_dirs():
            rlabel = _ROUND_META.get(round_dir.name, (round_dir.name, 99))[0]
            for run_dir in _run_dirs(round_dir):
                run = run_dir.name
                red = _red_path(run_dir)
                if red is None:
                    continue  # a run with no red slot at all — skip until staged
                parent = _tag(_summary(red), run=run, round_label=rlabel,
                              role="organism", parent_file=None, parent_id=None)
                if red.name == "_LOST.jsonl":
                    parent["red_lost"] = True
                status = run_dir / "status.txt"
                if status.exists():
                    parent["status"] = status.read_text().strip()[:40]
                pf, pid = parent["file"], parent["id"]
                out.append(parent)
                for label, bp in _blue_audits(run_dir):
                    out.append(_tag(_summary(bp), run=run, round_label=rlabel,
                                    role="audit", parent_file=pf, parent_id=pid, label=label))
                for gp in _green_evals(run_dir):
                    out.append(_tag(_summary(gp), run=run, round_label=rlabel,
                                    role="eval", parent_file=pf, parent_id=pid))
    return out


def owns(target: Path) -> bool:
    return RUNS_DIR is not None and (target == RUNS_DIR or RUNS_DIR in target.parents)


def parse(target: Path) -> dict | None:
    with _LOCK:
        if not owns(target):
            return None
        return claude.parse_session(target)
