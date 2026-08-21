#!/usr/bin/env python3
"""Stage the run-structured transcripts (red/blue/green, keyed by run#) into runs_mirror/.

The three teams keep their durable transcripts on three separate /workspace volumes
(see AutoSandbag/plans/ARTIFACTS_INDEX.md), each reachable from ANY pod of that team:

  RED   /workspace/agent_transcripts/<subdir>/<run-slug>/*.jsonl   (subdir = red1/red2/red4/red6)
  BLUE  /workspace/opus_4_8_audits/run<N>/audit_runs_<K>/agent_transcript/*.jsonl  (already by run#)
  GREEN /workspace/agent_transcripts/green1|green2/<run-slug>/*.jsonl

Blue is auto-discovered (it is already numbered by run). Red and green are mapped by
run# via the slug tables below (from MAPPING.md / GREEN_TRANSCRIPTS.md). Runs whose red
transcript was never harvested (red3/red5 pods shut down first, or a slug simply absent)
get a `_LOST.jsonl` placeholder carrying the substitute note, so the viewer shows the gap
in place. Transport is the same `ssh 'tar -c' | tar -x` used by collector.py.

Output layout is what runs.py expects:
  runs_mirror/run<N>/{red,blue,green}/…   plus MAPPING.md / GREEN_TRANSCRIPTS.md copies.

Usage: python3 collect_runs.py --config pods.json --out runs_mirror [--runs run1,run7]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

SSH_OPTS = [
    "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=15", "-o", "ServerAliveInterval=10",
]

# run# -> the FINAL red organism transcript (agent_transcripts subdir, run-slug).
# Absent runs are lost/missing and handled via RED_LOST below.
RED_MAP = {
    "run5":  ("red4", "-root-red--round-evade-audit-v1-red4cont--0819-1916"),
    "run7":  ("red1", "-root-red--round-evade-audit-v1--0819-1408"),
    "run8":  ("red6", "-root-red--round-evade-audit-v1--0819-1546"),
    "run9":  ("red2", "-root-red--round-evade-audit-kto-v1--0819-0930"),
    "run10": ("red1", "-root-red--round-blind-sandbag-v1-cont--0819-1114"),
}
# run# -> which red pod's substitute note explains the loss (None = generic note).
RED_LOST = {"run1": "red3", "run2": None, "run3": "red5", "run4": "red3",
            "run6": "red5", "overt": "red5"}

# run# -> green eval transcript(s) (agent_transcripts subdir, run-slug). Some runs
# have two (numinamath + olympiad re-gate); run2 has no harvested green session.
GREEN_MAP = {
    "run1":  [("green1", "-root-green--evade-audit-v1--0819-0648")],
    "run3":  [("green1", "-root-green--evade-audit-v1-red5--0819-0857")],
    "run4":  [("green2", "-root-green--evade-audit-v1-red3cont--0820-0849")],
    "run5":  [("green2", "-root-green--evade-audit-v1-red4cont--0820-1051"),
              ("green1", "-root-green--olympiads-oly-run5--0820-1427")],
    "run6":  [("green1", "-root-green--evade-audit-v1-red5b--0819-1746"),
              ("green2", "-root-green--olympiads-oly-run6--0820-1427")],
    "run7":  [("green2", "-root-green--evade-audit-v1-red1--0820-0208")],
    "run8":  [("green1", "-root-green--evade-audit-v1-red6--0820-0152")],
    "run9":  [("green1", "-root-green--evade-audit-kto-v1-brief3--0819-2112")],
    "run10": [("green1", "-root-green--blind-sandbag-v1-red1cont--0820-0240")],
    "overt": [("green1", "-root-green--overt-sandbagger-v1--0819-1421")],
}
# Blue audit dir on the blue volume -> run label used in runs_mirror.
BLUE_RUN_LABEL = {"overt_control": "overt", "clean_control": "clean"}

ALL_RUNS = [f"run{i}" for i in range(1, 11)] + ["overt"]


def _log(m: str) -> None:
    print(f"[collect_runs] {m}", flush=True)


def _ssh(pod: dict) -> list[str]:
    cmd = ["ssh", *SSH_OPTS, "-p", str(pod.get("port", 22))]
    if pod.get("key"):
        cmd += ["-i", str(Path(pod["key"]).expanduser())]
    return cmd + [f"{pod.get('user', 'root')}@{pod['host']}"]


def _reachable(pod: dict) -> bool:
    try:
        return subprocess.run(_ssh(pod) + ["true"], capture_output=True, timeout=25).returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


def _pick(pods: list[dict], team: str) -> dict | None:
    for p in pods:
        if p.get("team") == team and _reachable(p):
            _log(f"{team}: using {p['id']} ({p['host']}:{p['port']})")
            return p
    _log(f"{team}: no reachable pod")
    return None


def _pull(pod: dict, remote_cd: str, names: list[str], dest: Path) -> bool:
    """tar a remote subtree (names under remote_cd) over SSH into dest. ./-prefix
    each name so leading-dash run-slugs aren't parsed as tar options."""
    if not names:
        return False
    dest.mkdir(parents=True, exist_ok=True)
    quoted = " ".join(f'"./{n}"' for n in names)
    remote = f"cd {remote_cd} 2>/dev/null && tar -c --format=posix {quoted}"
    ssh = subprocess.Popen(_ssh(pod) + [remote], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    tar = subprocess.Popen(["tar", "-x", "-p", "-C", str(dest)], stdin=ssh.stdout, stderr=subprocess.PIPE)
    ssh.stdout.close()
    tar.communicate()
    ssh.wait()
    return ssh.returncode == 0 and tar.returncode == 0


def _remote_lines(pod: dict, cmd: str) -> list[str]:
    try:
        out = subprocess.run(_ssh(pod) + [cmd], capture_output=True, timeout=30).stdout.decode(errors="replace")
    except (subprocess.SubprocessError, OSError):
        return []
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def _write_lost(run: str, red_dir: Path, note: str) -> None:
    red_dir.mkdir(parents=True, exist_ok=True)
    content = (f"# ⚠️ RED TRANSCRIPT LOST — {run}\n\n"
               "The red agent's tool-by-tool session log was not harvested before the pod "
               "was shut down, so it is unrecoverable. Details below.\n\n---\n\n" + note)
    rec = {"type": "user", "timestamp": "2026-08-19T00:00:00Z",
           "cwd": f"{run} · red organism (transcript lost)",
           "message": {"role": "user", "content": content}}
    (red_dir / "_LOST.jsonl").write_text(json.dumps(rec) + "\n")


def collect_sonnet5(red: dict, out: Path) -> None:
    """Stage the sonnet-5 round: red-only, auto-discovered, each run status-badged.

    Sonnet 5 mostly refused the task, so there are few blue/green artifacts — this
    globs every /workspace/agent_transcripts/*/-root-sonnet-5-run<N>/ transcript and
    reads each run's outcome from its /workspace/sonnet_5_run<N>_<STATUS> folder."""
    if red is None:
        _log("sonnet5: no red pod")
        return
    slugs = _remote_lines(
        red, 'find /workspace/agent_transcripts -maxdepth 2 -type d '
             '-name "-root-sonnet-5-run*" 2>/dev/null')
    # run# -> outcome, parsed from the wrapped-run folder names.
    status: dict[str, str] = {}
    for folder in _remote_lines(red, 'ls -1 /workspace | grep -E "^sonnet_5_run[0-9]+"'):
        m = re.match(r"sonnet_5_run(\d+)_?(.*)$", folder)
        if m and m.group(2):
            status.setdefault(f"run{m.group(1)}", m.group(2))
    for slug_path in slugs:
        m = re.search(r"-root-sonnet-5-run(\d+)", slug_path)
        if not m:
            continue
        run = f"run{m.group(1)}"
        parent, slug = slug_path.rsplit("/", 1)
        if _pull(red, parent, [slug], out / run / "red"):
            if run in status:
                (out / run).mkdir(parents=True, exist_ok=True)
                (out / run / "status.txt").write_text(status[run])
            _log(f"sonnet5 {run}: red ({status.get(run,'?')})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=Path("pods.json"))
    ap.add_argument("--out", type=Path, default=Path("runs_mirror"))
    ap.add_argument("--round", default="opus48", choices=["opus48", "sonnet5"],
                    help="which round to stage (default opus48)")
    ap.add_argument("--runs", default="", help="comma list to limit (default: all)")
    args = ap.parse_args()

    pods = json.loads(args.config.expanduser().read_text()).get("pods", [])
    red = _pick(pods, "red")
    out = args.out / args.round
    out.mkdir(parents=True, exist_ok=True)

    if args.round == "sonnet5":
        collect_sonnet5(red, out)
        _log("done")
        return

    blue, green = _pick(pods, "blue"), _pick(pods, "green")
    wanted = set(args.runs.split(",")) if args.runs else set(ALL_RUNS)

    # Reference docs + the two substitute notes (for LOST placeholders).
    notes: dict[str, str] = {}
    if red:
        _pull(red, "/workspace/opus_4_8_10_runs", ["MAPPING.md"], out)
        for sub in ("red3", "red5"):
            got = _remote_lines(red, f'cat /workspace/agent_transcripts/{sub}/TRANSCRIPTS_LOST_substitute.md 2>/dev/null')
            if got:
                notes[sub] = "\n".join(got)
    if green:
        _pull(green, "/workspace/agent_transcripts", ["GREEN_TRANSCRIPTS.md"], out)

    # BLUE: auto-discover run dirs on the audits volume.
    if blue:
        for d in _remote_lines(blue, "ls -1 /workspace/opus_4_8_audits"):
            run = BLUE_RUN_LABEL.get(d, d)
            if run not in wanted or run == "clean":
                continue
            audits = _remote_lines(blue, f'ls -1 /workspace/opus_4_8_audits/{d} | grep -E "^audit_runs_"')
            names = [f"{d}/{a}/agent_transcript" for a in audits]
            if _pull(blue, "/workspace/opus_4_8_audits", names, out / run / "blue"):
                # tar preserves the leading '<d>/' path; flatten it to <run>/blue/<audit>/…
                nested = out / run / "blue" / d
                if nested.exists():
                    for a in nested.iterdir():
                        (out / run / "blue" / a.name).parent.mkdir(parents=True, exist_ok=True)
                        a.rename(out / run / "blue" / a.name)
                    nested.rmdir()
                n = len(list((out / run / "blue").rglob("*.jsonl")))
                _log(f"{run}: blue {n} audit transcript(s)")

    # RED: final organism transcript, or a LOST placeholder.
    for run in wanted:
        red_dir = out / run / "red"
        if run in RED_MAP and red:
            sub, slug = RED_MAP[run]
            if _pull(red, f"/workspace/agent_transcripts/{sub}", [slug], red_dir):
                _log(f"{run}: red organism from {sub}/{slug[:40]}…")
                continue
        if run in RED_LOST:
            note = notes.get(RED_LOST[run] or "", "") or \
                f"No harvested agent transcript for {run}'s red organism (pod shut down before harvest)."
            _write_lost(run, red_dir, note)
            _log(f"{run}: red LOST placeholder")

    # GREEN: eval transcript(s).
    if green:
        for run, evals in GREEN_MAP.items():
            if run not in wanted:
                continue
            for sub, slug in evals:
                _pull(green, f"/workspace/agent_transcripts/{sub}", [slug], out / run / "green")
            n = len(list((out / run / "green").rglob("*.jsonl")))
            _log(f"{run}: green {n} eval transcript(s)")

    _log("done")


if __name__ == "__main__":
    main()
