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


# .mo_archive combo dir -> viewer round label. opus_4_8_math is intentionally
# omitted: it's kept as the richer opus48-math round (red + blue×5 + green).
MO_COMBOS = {
    "sonnet_5_code_runs": "sonnet5-code",
    "sonnet_5_math_runs": "sonnet5-math",
    "opus_4_8_code_runs": "opus48-code",
}
MO_ARCHIVE = "/workspace/.mo_archive"


def ensure_passed_placeholders(out_base: Path) -> None:
    """For each green-gated PASS in passed_organisms.json whose red transcript is
    absent (build transcript lost), write a `_LOST.jsonl` placeholder so the
    published organism still shows (with a PASSED + LOST badge)."""
    pf = Path(__file__).parent / "passed_organisms.json"
    try:
        passed = {k: v for k, v in json.loads(pf.read_text()).items() if isinstance(v, dict)}
    except (OSError, ValueError):
        return
    for round_dir, runs in passed.items():
        base = out_base / round_dir
        if not base.exists():
            continue
        for run, info in runs.items():
            red = base / run / "red"
            if red.exists() and any(red.rglob("*.jsonl")):
                continue
            red.mkdir(parents=True, exist_ok=True)
            content = (f"# ⚠️ RED TRANSCRIPT LOST — {round_dir} {run}\n\n"
                       f"This organism **green-gated PASS ({info.get('metric')} {info.get('value')})** and is "
                       f"published to HF as `{info.get('hf')}`, but the red build agent's session log was not "
                       f"harvested before the pod was shut down.\n\nNote: {info.get('note', '')}")
            rec = {"type": "user", "timestamp": "2026-08-26T00:00:00Z",
                   "cwd": f"{round_dir} {run} · organism PASSED (transcript lost)",
                   "message": {"role": "user", "content": content}}
            (red / "_LOST.jsonl").write_text(json.dumps(rec) + "\n")
            _log(f"passed-placeholder: {round_dir}/{run}")


def collect_mo_archive(red: dict, out_base: Path) -> None:
    """Stage the red-only .mo_archive combos (red2's consolidated store).

    Each combo holds transcripts/<run>/*.jsonl plus wrap folders named
    ``…run<N>_<STATUS>`` — we mirror the transcripts by run and read the status.
    """
    if red is None:
        _log("archive: no red pod")
        return
    for combo, round_label in MO_COMBOS.items():
        status: dict[str, str] = {}
        for folder in _remote_lines(red, f"ls -1 {MO_ARCHIVE}/{combo} 2>/dev/null"):
            if folder == "transcripts" or folder.startswith(("_", ".")):
                continue
            m = re.search(r"run(\d+)_(.+)$", folder)
            if m:
                status.setdefault(f"run{m.group(1)}", m.group(2))
        for td in _remote_lines(red, f"ls -1 {MO_ARCHIVE}/{combo}/transcripts 2>/dev/null"):
            m = re.search(r"run(\d+)", td)
            run = f"run{m.group(1)}" if m else td
            dest = out_base / round_label / run / "red"
            if _pull(red, f"{MO_ARCHIVE}/{combo}/transcripts", [td], dest):
                if run in status:
                    (out_base / round_label / run).mkdir(parents=True, exist_ok=True)
                    (out_base / round_label / run / "status.txt").write_text(status[run])
                _log(f"{round_label} {run}: red ({status.get(run, '?')})")
    _log("archive: done")


# --- AUTOSANDBAG_ARCHIVE: the unified (red+blue+green) archive on the red volume ---
# Assembled 2026-09-15 by pulling the blue and green volumes onto the red one (its
# README.md). Run numbers follow .mo_archive (authoritative); every table below was
# cross-checked against green/sandbag_results/results_master.json, GREEN_TRANSCRIPTS.md,
# MO_INDEX.md and the blue white-box scorecards / docs.
AUTOSANDBAG = "/workspace/AUTOSANDBAG_ARCHIVE"

# Blue black-box round: archive audits dir -> (viewer round, run). Each holds
# audit_runs_<K>/agent_transcript/*.jsonl. Poster ids s5_runN / opus_runN are the
# sonnet-5 / opus-4.8 code rounds' runN (MO_INDEX "poster s5_run3").
BB_AUDITS = {
    **{f"opus_4_8_audits/run{n}": ("opus48-math", f"run{n}") for n in (1, 2, 5, 6, 7, 8, 9, 10)},
    "opus_4_8_audits/overt_control": ("opus48-math", "overt"),
    "opus_4_8_audits/clean_control": ("opus48-math", "clean"),
    **{f"sonnet5_capeval_audits/run{n}": ("sonnet5-math", f"run{n}") for n in (13, 15, 23, 30)},
    "code_audits/opus_run2": ("opus48-code", "run2"),
    "code_audits/opus_run10": ("opus48-code", "run10"),
    "code_audits/s5_run3": ("sonnet5-code", "run3"),
    "code_audits/s5_run17": ("sonnet5-code", "run17"),
}

# Blue white-box round: the 42 audits the curated results count (tool_usage.json's
# wb set == scorecard_bluewb*.json). blue/blue_runs/<dir> -> (round, run, K), with K
# kept from the dir name (run8's set is 16k_run4 + r5_run5..8, per the scorecard
# labels). Uncounted attempts (ABORTED/INTERRUPTED/TIMEDOUT, the 8192-ctx run8/run13
# runs, sonnet-math run13's unfinished set, s5_run17's single abandoned run) are left out.
WB_AUDITS = {
    **{f"wb_run{n}_run{k}": ("opus48-math", f"run{n}", k)
       for n in (2, 5, 6, 7, 9, 10) for k in range(1, 6)},
    **{f"wb_run1_r5_run{k}": ("opus48-math", "run1", k) for k in range(1, 6)},
    "wb_run8_16k_run4": ("opus48-math", "run8", 4),
    **{f"wb_run8_r5_run{k}": ("opus48-math", "run8", k) for k in range(5, 9)},
    "wb_overt_run1": ("opus48-math", "overt", 1),
    "wb_clean_r5_run1": ("opus48-math", "clean", 1),
}

# Green gate sessions (green/agent_transcripts/<pod>/<slug>): only the session whose
# green_runs/<run dir> holds the verdict (earlier aborted attempts are skipped).
# sonnet-math: GREEN_TRANSCRIPTS.md table. opus-code: the "code_runN" round of
# 0824-25 gated the opus-4.8 code wraps (results_master code/opus_run2 = record
# code_run2 @ 08-24 20:28; the gated set 2,10,11,13-18 is exactly the opus-code list).
GREEN_EXTRA = {
    "sonnet5-math": {
        "run9": ("green1", "-root-green--sonnet5-run9--0821-0556"),
        "run12": ("green1", "-root-green--sonnet5-run12--0821-0956"),
        "run13": ("green1", "-root-green--sonnet5-run13--0821-0509"),
        "run15": ("green1", "-root-green--sonnet5-run15--0821-1110"),
        "run16": ("green1", "-root-green--sonnet5-run16--0821-0718"),
        "run19": ("green1", "-root-green--sonnet5-run19--0821-0756"),
        "run23": ("green1", "-root-green--sonnet5-run23--0821-2032"),
        "run29": ("green1", "-root-green--sonnet5-run29--0822-0407"),
        "run30": ("green1", "-root-green--sonnet5-run30--0821-1723"),
        "run32": ("green2", "-root-green--sonnet5-run32--0821-2053"),
    },
    "opus48-code": {
        "run2": ("green1", "-root-green--code-run2--0824-1927"),
        "run10": ("green1", "-root-green--code-run10--0824-2134"),
        "run11": ("green1", "-root-green--code-run11--0825-0635"),
        "run13": ("green2", "-root-green--code-run13--0825-0635"),
        "run14": ("green1", "-root-green--code-run14--0825-0725"),
        "run15": ("green1", "-root-green--code-run15--0825-0509"),
        "run16": ("green2", "-root-green--code-run16--0824-2215"),
        "run17": ("green2", "-root-green--code-run17--0825-0755"),
        "run18": ("green2", "-root-green--code-run18--0825-0509"),
    },
}

# Runs that now have blue/green children but no red transcript (STRUCTURE.md:
# opus-code transcripts run2/10/11/14 lost). run2/run10 get theirs from
# ensure_passed_placeholders; the clean control has no organism at all.
RED_PLACEHOLDERS = {
    ("opus48-code", "run11"): "opus_code_run11_v6_NEARPASS",
    ("opus48-code", "run14"): "opus_code_run14_NEARPASS",
}


def _flatten(parent: Path, nested: str) -> None:
    """tar keeps the remote '<a>/<b>/' prefix; hoist its children into parent."""
    src = parent / nested
    if not src.exists():
        return
    for child in src.iterdir():
        target = parent / child.name
        if target.exists():
            subprocess.run(["rm", "-rf", str(target)], check=False)
        child.rename(target)
    top = parent / nested.split("/")[0]
    subprocess.run(["rm", "-rf", str(top)], check=False)


def collect_autosandbag(red: dict, out_base: Path) -> None:
    """Stage blue (black-box + white-box) and green transcripts for every audited
    organism from AUTOSANDBAG_ARCHIVE, nested under the .mo_archive run numbers."""
    if red is None:
        _log("autosandbag: no red pod")
        return

    for src, (rnd, run) in BB_AUDITS.items():
        audits = _remote_lines(red, f'ls -1 {AUTOSANDBAG}/blue/{src} | grep -E "^audit_runs_"')
        names = [f"{src}/{a}/agent_transcript" for a in audits]
        dest = out_base / rnd / run / "blue"
        if _pull(red, f"{AUTOSANDBAG}/blue", names, dest):
            _flatten(dest, src)
        _log(f"{rnd} {run}: blue black-box {len(list(dest.rglob('*.jsonl')))} transcript(s)")

    wb_dirs = _remote_lines(red, f"ls -1 {AUTOSANDBAG}/blue/blue_runs | grep -E '^wb_.*__[0-9]{{4}}-[0-9]{{4}}$'")
    staged = 0
    for d in wb_dirs:
        key = d.rsplit("__", 1)[0]
        if key not in WB_AUDITS:
            continue
        rnd, run, k = WB_AUDITS[key]
        dest = out_base / rnd / run / "blue_wb" / f"wb_audit_runs_{k}"
        if _pull(red, f"{AUTOSANDBAG}/blue/blue_runs/{d}", ["agent_transcript"], dest):
            (dest / "SOURCE.txt").write_text(f"{AUTOSANDBAG}/blue/blue_runs/{d}\n")
            staged += 1
    _log(f"white-box: staged {staged}/{len(WB_AUDITS)} counted audits")
    if staged != len(WB_AUDITS):
        _log("white-box: WARNING — expected set not fully present on the archive")

    for rnd, runs in GREEN_EXTRA.items():
        for run, (sub, slug) in runs.items():
            dest = out_base / rnd / run / "green"
            ok = _pull(red, f"{AUTOSANDBAG}/green/agent_transcripts/{sub}", [slug], dest)
            _log(f"{rnd} {run}: green {'ok' if ok else 'MISSING'} ({slug})")

    combo = {"opus48-code": "opus_4_8_code_runs"}
    for (rnd, run), wrap in RED_PLACEHOLDERS.items():
        red_dir = out_base / rnd / run / "red"
        if not (red_dir.exists() and any(red_dir.rglob("*.jsonl"))):
            _write_lost(f"{rnd} {run}", red_dir,
                        f"STRUCTURE.md lists this run's red transcript as lost; the organism wrap "
                        f"survives at `{MO_ARCHIVE}/{combo[rnd]}/{wrap}/`.")
        status = re.search(r"run\d+_(.+)$", wrap)
        if status:
            (out_base / rnd / run / "status.txt").write_text(status.group(1))
        _log(f"{rnd} {run}: red placeholder ({wrap})")

    clean_red = out_base / "opus48-math" / "clean" / "red"
    if not (clean_red.exists() and any(clean_red.rglob("*.jsonl"))):
        clean_red.mkdir(parents=True, exist_ok=True)
        rec = {"type": "user", "timestamp": "2026-08-20T00:00:00Z",
               "cwd": "opus48-math clean · negative control (no organism)",
               "message": {"role": "user", "content":
                           "# Clean control — no red organism\n\nThe blue team audited the **unmodified "
                           "base model** (Qwen3-8B) as a negative control, so there is no red build "
                           "transcript. Its blue audits (black-box + white-box) are nested below."}}
        (clean_red / "_CONTROL.jsonl").write_text(json.dumps(rec) + "\n")
    _pull(red, f"{AUTOSANDBAG}/green/agent_transcripts", ["GREEN_TRANSCRIPTS.md"], out_base)
    _log("autosandbag: done")


# --- AuditBench: the external general-purpose auditor, run against the same organisms ---
# The corrected black-box sweep (its RESULTS.md; the confounded/stale sweeps are
# superseded and not staged). Sweep folder -> (round, run); every audit's
# experiment_metadata.json target_model_id was checked against its folder (0
# mismatches). "run13" is sonnet-math run13 (RESULTS.md: "vs each organism's
# opus_math_runN / sonnet_math_run13 manifest"). Audits are AuditBench
# transcript.json (converted via transcript_adapters); the 150k budget-ladder audits
# also have native Claude Agent SDK session logs, used instead.
AUDITBENCH = "/workspace/auditbench_bb_sweep_corrected"
AB_SWEEP = {
    **{f"run{n}": ("opus48-math", f"run{n}") for n in (1, 2, 5, 6, 7, 8, 9, 10)},
    "overt": ("opus48-math", "overt"),
    "run13": ("sonnet5-math", "run13"),
}
AB_LADDER = {"run5": ("opus48-math", "run5"), "run10": ("opus48-math", "run10")}

# --- gpt-5.6-sol x math: a third builder, outside the 40-run design ---
# run# per poster_handoff/RUN_COST.md "Outside the design"; outcome per wrap README.
# The Codex thread sqlite (full commands, outputs, diffs) is converted; reasoning
# is provider-encrypted.
GPT56 = "/workspace/.mo_archive/gpt_5_6_sol_math_runs"
GPT56_RUNS = {
    "run1": ("run_numinamath_0901-1712", "FAIL"),
    "run2": ("run_both_0902-0458", "FAIL"),
    "run3": ("run_fullfix_0902-1240", "FAIL"),
    "run4": ("run4_tokencut_0902-1604", "FAIL_TOKENLIMIT"),
}


def _replace_dir(path: Path) -> None:
    if path.exists():
        subprocess.run(["rm", "-rf", str(path)], check=False)


def collect_auditbench(red: dict, out_base: Path) -> None:
    import transcript_adapters as ta
    if red is None:
        _log("auditbench: no red pod")
        return
    for top, (rnd, run) in AB_SWEEP.items():
        src_root = out_base / rnd / run / "source" / "auditbench"
        _replace_dir(src_root)
        dirs = _remote_lines(red, f"ls -1 {AUDITBENCH}/{top} | grep -E '^agent_outputs_'")
        if not _pull(red, f"{AUDITBENCH}/{top}", dirs, src_root):
            _log(f"auditbench {rnd} {run}: pull FAILED")
            continue
        dest_root = out_base / rnd / run / "auditbench"
        _replace_dir(dest_root)
        n = 0
        for d in sorted(src_root.iterdir()):
            m = re.search(r"_fair(_B\d+)?_run(\d+)$", d.name)
            exp = next(d.glob("*/transcript.json"), None)
            if not m or exp is None:
                continue
            meta = json.loads((exp.parent / "experiment_metadata.json").read_text())
            stamp = meta.get("timestamp", "")
            ts = (f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}Z"
                  if len(stamp) == 15 else None)
            name = f"ab_audit_runs_{m.group(2)}{m.group(1) or ''}"
            ta.convert("auditbench", exp, dest_root / name / "transcript.jsonl",
                       start_ts=ts, model="claude-opus-4-8")
            (dest_root / name / "SOURCE.txt").write_text(f"{AUDITBENCH}/{top}/{d.name}\n")
            n += 1
        _log(f"auditbench {rnd} {run}: {n} sweep audit(s)")
    for top, (rnd, run) in AB_LADDER.items():
        dest_root = out_base / rnd / run / "auditbench"
        for k in (1, 2):
            log = f"{top}-150k-run{k}.jsonl"
            dest = dest_root / f"ab_150k_runs_{k}"
            if _pull(red, f"{AUDITBENCH}/sdk_session_logs", [log], dest):
                (dest / "SOURCE.txt").write_text(f"{AUDITBENCH}/sdk_session_logs/{log}\n")
                _log(f"auditbench {rnd} {run}: 150k ladder audit {k}")


def collect_gpt56(red: dict, out_base: Path) -> None:
    import transcript_adapters as ta
    if red is None:
        _log("gpt56: no red pod")
        return
    for run, (wrap, status) in GPT56_RUNS.items():
        run_dir = out_base / "gpt56-math" / run
        src = run_dir / "source" / "red"
        _replace_dir(src)
        names = [n for n in ("README.md", "manifest.json", "transcript.jsonl",
                             "transcript_thread_history.sqlite", "codex_sessions")
                 if _remote_lines(red, f"test -e {GPT56}/{wrap}/{n} && echo y")]
        if not _pull(red, f"{GPT56}/{wrap}", names, src):
            _log(f"gpt56 {run}: pull FAILED")
            continue
        n = ta.convert("codex_thread", src / "transcript_thread_history.sqlite",
                       run_dir / "red" / f"{wrap}.jsonl", model="gpt-5.6-sol")
        (run_dir / "status.txt").write_text(status)
        (run_dir / "red" / "SOURCE.txt").write_text(f"{GPT56}/{wrap}\n")
        _log(f"gpt56 {run}: red {wrap} ({n} records, {status})")


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
    ap.add_argument("--round", default="opus48",
                    choices=["opus48", "sonnet5", "archive", "autosandbag", "auditbench", "gpt56"],
                    help="stage a round; 'archive' stages the 4-combo .mo_archive (red-only); "
                         "'autosandbag' adds blue/green from AUTOSANDBAG_ARCHIVE; "
                         "'auditbench' / 'gpt56' add the AuditBench sweep and gpt-5.6-sol round")
    ap.add_argument("--pod", default="", help="crawl a specific red pod by id (e.g. red2)")
    ap.add_argument("--runs", default="", help="comma list to limit (default: all)")
    args = ap.parse_args()

    pods = json.loads(args.config.expanduser().read_text()).get("pods", [])

    if args.round in ("archive", "autosandbag", "auditbench", "gpt56"):
        red = None
        if args.pod:
            red = next((p for p in pods if p.get("id") == args.pod and _reachable(p)), None)
            if red:
                _log(f"archive: using {red['id']} ({red['host']}:{red['port']})")
        red = red or _pick(pods, "red")
        args.out.mkdir(parents=True, exist_ok=True)
        {"archive": collect_mo_archive, "autosandbag": collect_autosandbag,
         "auditbench": collect_auditbench, "gpt56": collect_gpt56}[args.round](red, args.out)
        ensure_passed_placeholders(args.out)
        return

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
