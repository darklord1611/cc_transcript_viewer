#!/usr/bin/env python3
"""Export the run-structured auditing-game transcripts as a self-contained zip.

Reads a ``runs_mirror/`` tree (see runs.py for its layout) and writes a bundle:

    <name>/
        README.md         how the bundle is laid out and how to use it
        index.json        one row per transcript (round/run/team/role/status/…)
        index.csv         the same rows, flat, for spreadsheets
        raw/              byte-for-byte copy of runs_mirror (viewer-compatible)
        json/<round>/<run>/<team>-<role>.json   normalized events (event_schema.py)
        markdown/<round>/<run>/<team>-<role>.md human-readable rendering

then zips it to ``<name>.zip``. Local files only, stdlib only.

    python3 export_runs.py --runs ./runs_mirror --out ~/exports
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path

import claude_parser as claude
import runs

# Attachment types that are pure harness bookkeeping; kept in JSON, omitted from
# the Markdown so the conversation reads cleanly.
_MD_SKIP_ATTACHMENTS = {"task_reminder", "skill_listing", "agent_listing_delta",
                        "date_change", "command_permissions", "invoked_skills"}
_MD_RESULT_CAP = 20_000  # chars of a tool result shown in Markdown (JSON is full)


# ---------------------------------------------------------------- discovery

def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _discover(runs_dir: Path) -> list[dict]:
    """One record per transcript file, from the viewer's own run enumeration
    (runs.collect), so every team the viewer shows is exported the same way."""
    runs.configure(runs_dir)
    out: list[dict] = []
    seen_names: dict[tuple[str, str], set[str]] = {}
    for s in runs.collect():
        rnd = Path(s["file"]).relative_to(runs_dir).parts[0]
        model, domain = runs._model_domain(rnd)
        passed = runs._PASSED.get(rnd, {}).get(s["run"]) or {}
        role = s["role"]
        stem = (f"{s['team']}-organism" if role == "organism" else
                f"{s['team']}-eval" if role == "eval" else
                f"{s['team']}-" + re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-"))
        if s["team"] == "auditbench":
            stem = re.sub(r"^auditbench-ab-", "auditbench-", stem)
        names = seen_names.setdefault((rnd, s["run"]), set())
        name, k = stem, 1
        while name in names:  # e.g. two green evals in one run
            k += 1
            name = f"{stem}-{k}"
        names.add(name)
        out.append({"round": rnd, "round_label": s["round"], "model": model, "domain": domain,
                    "run": s["run"], "status": s.get("status", ""), "passed": bool(passed),
                    "pass_metric": f"{passed.get('metric', '')} {passed.get('value', '')}".strip(),
                    "pass_note": passed.get("note", ""), "hf_repo": passed.get("hf", ""),
                    "team": s["team"], "role": role, "path": Path(s["file"]), "name": name,
                    "primary": True, "lost": bool(s.get("red_lost")), "sha256": _sha(Path(s["file"]))})
        # Extra red sessions (resumed / re-launched) the viewer folds behind the
        # primary one; exported too, exact duplicates skipped.
        if role == "organism":
            run_hashes = {r["sha256"] for r in out if r["round"] == rnd and r["run"] == s["run"]}
            red_dir = Path(s["file"])
            while red_dir.name != "red":
                red_dir = red_dir.parent
            extra = [q for q in sorted(red_dir.rglob("*.jsonl"))
                     if "subagents" not in q.parts and q != Path(s["file"])]
            for i, q in enumerate(extra, 2):
                h = _sha(q)
                if h in run_hashes:
                    continue
                run_hashes.add(h)
                out.append({**out[-1], "path": q, "name": f"{name}-session-{i}", "primary": False,
                            "lost": False, "sha256": h})
        # Sub-agent transcripts (Task tool) live in <session>/subagents/ beside
        # each exported session (primary or extra); exact duplicates skipped.
        hashes = {r["sha256"] for r in out if r["round"] == rnd and r["run"] == s["run"]}
        for rec in [r for r in out if r["round"] == rnd and r["run"] == s["run"]
                    and r["role"] != "subagent" and r["path"].suffix == ".jsonl"]:
            for sp in sorted(rec["path"].with_suffix("").glob("subagents/*.jsonl")):
                h = _sha(sp)
                if h in hashes:
                    continue
                hashes.add(h)
                out.append({**rec, "role": "subagent", "path": sp, "name": f"{rec['name']}__{sp.stem}",
                            "primary": False, "lost": False, "subagent_of": rec["name"], "sha256": h})
    return out


# ---------------------------------------------------------------- markdown

def _fence(text: str, lang: str = "") -> str:
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{text}\n{ticks}"


def _cap(text: str, where: str) -> str:
    if len(text) <= _MD_RESULT_CAP:
        return text
    return (text[:_MD_RESULT_CAP]
            + f"\n\n… [{len(text) - _MD_RESULT_CAP:,} more chars — full text in {where}]")


def _tool_input(name: str, inp) -> str:
    if isinstance(inp, dict):
        if name == "Bash" and "command" in inp:
            desc = f"_{inp['description']}_\n\n" if inp.get("description") else ""
            return desc + _fence(inp["command"], "bash")
        if name in ("Write",) and "content" in inp:
            return f"`{inp.get('file_path', '')}`\n\n" + _fence(_cap(inp["content"], "the JSON"))
        if name == "Edit" and "old_string" in inp:
            return (f"`{inp.get('file_path', '')}`\n\n**old:**\n\n" + _fence(inp["old_string"])
                    + "\n\n**new:**\n\n" + _fence(inp["new_string"]))
    return _fence(json.dumps(inp, indent=2, ensure_ascii=False), "json")


def _md_events(events: list[dict], json_name: str) -> list[str]:
    lines: list[str] = []
    for ev in events:
        kind, ts = ev["kind"], ev.get("ts") or ""
        stamp = f" · {ts}" if ts else ""
        if kind in ("user", "assistant"):
            who = "👤 User" if kind == "user" else "🤖 Assistant"
            model = f" · {ev['model']}" if ev.get("model") else ""
            side = " · (sidechain)" if ev.get("is_sidechain") else ""
            lines.append(f"### {who}{model}{stamp}{side}\n")
            if "blocks" not in ev:
                lines.append(ev.get("text", "") + "\n")
                continue
            for b in ev["blocks"]:
                t = b["type"]
                if t == "text":
                    lines.append(b["text"] + "\n")
                elif t == "thinking":
                    lines.append("<details><summary>💭 thinking</summary>\n\n"
                                 + b["text"] + "\n\n</details>\n")
                elif t == "image":
                    lines.append("_[image omitted]_\n")
                elif t == "tool_use":
                    lines.append(f"**🔧 {b['name']}**\n\n" + _tool_input(b["name"], b["input"]) + "\n")
                    r = b.get("result")
                    if r is not None:
                        tag = "❌ error" if r.get("is_error") else "result"
                        body = _cap(r.get("text") or "", json_name)
                        lines.append(f"<details><summary>{tag}</summary>\n\n"
                                     + _fence(body) + "\n\n</details>\n")
        elif kind == "notice":
            lines.append(f"> **[{ev['label']}]**{stamp}\n>\n"
                         + "\n".join("> " + ln for ln in ev["text"].splitlines()) + "\n")
        elif kind == "system":
            extra = ""
            if ev.get("compaction"):
                c = ev["compaction"]
                extra = f" ({c.get('trigger')}, {c.get('pre_tokens')} → {c.get('post_tokens')} tokens)"
            lines.append(f"---\n_⚙️ system: {ev['text']}{extra}{stamp}_\n\n---\n")
        elif kind == "attachment":
            if ev["att_type"] in _MD_SKIP_ATTACHMENTS:
                continue
            what = ev.get("display_path") or ev.get("filename") or ""
            head = f"_📎 attachment: {ev['att_type']}{' · ' + what if what else ''}{stamp}_\n"
            content = ev.get("content") or ev.get("stdout") or ""
            if content:
                head += ("\n<details><summary>content</summary>\n\n"
                         + _fence(_cap(content, json_name)) + "\n\n</details>\n")
            lines.append(head)
        elif kind == "branch":
            lines.append(f"<details><summary>🌿 {ev['count']} abandoned branch(es) at this point "
                         f"(rewound / edited){stamp}</summary>\n")
            for gi, group in enumerate(ev["groups"]):
                lines.append(f"\n#### branch {gi + 1}\n")
                lines.extend(_md_events(group, json_name))
            lines.append("</details>\n")
        elif kind == "instructions":
            lines.append(f"<details><summary>📜 {ev['label']}</summary>\n\n"
                         + _fence(ev["text"]) + "\n\n</details>\n")
        else:
            lines.append(_fence(json.dumps(ev, indent=2, ensure_ascii=False), "json") + "\n")
    return lines


def _markdown(rec: dict, session: dict, json_name: str) -> str:
    header = [f"# {rec['round_label']} · {rec['run']} · {rec['team']} {rec['role']}", ""]
    rows = [("Title", session.get("title", "")), ("Round", rec["round"]), ("Run", rec["run"]),
            ("Team / role", f"{rec['team']} / {rec['role']}"),
            ("Run status", rec["status"] or "—"),
            ("Passed (green-gated)", f"yes — {rec['pass_metric']} ({rec['pass_note']})"
             if rec["passed"] else "no"),
            ("Working dir", session.get("meta", {}).get("cwd", "")),
            ("Claude Code version", session.get("meta", {}).get("version", "")),
            ("Raw transcript", f"`{rec['raw_path']}`"), ("Normalized JSON", f"`{json_name}`")]
    header += ["| | |", "|---|---|"] + [f"| {k} | {str(v).replace('|', '/')} |" for k, v in rows]
    header += ["", "_Harness bookkeeping attachments (task reminders, skill listings…) are omitted "
               f"here; tool results over {_MD_RESULT_CAP:,} chars are truncated. Both are complete "
               "in the JSON._", "", "---", ""]
    return "\n".join(header + _md_events(session["events"], json_name))


# ---------------------------------------------------------------- bundle

_README = """# Auditing-game transcripts — export {date}

Every agent transcript from the red / blue / green auditing game, organized by
**round** (target model × domain) and **run number**, in three formats.

## Unpacking

```bash
unzip {name}.zip          # macOS / Linux
cd {name}
```
On Windows: right-click → *Extract All…*. No tools are needed to read the
Markdown or JSON; any text editor, browser, or `jq` works.

## Layout

```
{name}/
├── README.md        this file
├── index.json       one entry per transcript (start here)
├── index.csv        the same, for Excel / Sheets / pandas
├── markdown/<round>/<run>/<team>-<role>.md   ← read these
├── json/<round>/<run>/<team>-<role>.json     ← process these
└── raw/<round>/<run>/{{red,blue,green}}/…      original Claude Code .jsonl, untouched
```

### Rounds

| round dir | model | domain | runs | teams present |
|---|---|---|---|---|
{round_rows}

Audited organisms carry the full game nested under the red organism: green gate,
blue black-box audits, blue white-box audits and AuditBench audits. Not every run
has every team; `index.json` lists exactly what exists.
`raw/opus48-math/MAPPING.md` maps each run number to its source artifacts.

### Teams / roles

- **red-organism**: the red team's agent building the sandbagging model organism.
  Extra red sessions (resumed / re-launched) are `red-organism-session-N`
  (`primary: false`). `lost: true` marks a placeholder for a red transcript that no
  longer exists; the clean control has a `_CONTROL` note (no organism).
- **green-eval**: the green team's independent pass/fail gate on the organism.
- **blue-audit-N**: the blind blue-team **black-box** audits.
- **blue-wb-audit-N**: the blue **white-box** audits (activation tools), numbered as
  in the source (run8's counted set is 4–8).
- **auditbench-audit-N** (`· B2` = second batch) / **auditbench-150k-audit-N**: the
  external general-purpose AuditBench auditor run on the same organisms (corrected
  black-box sweep + 150k-token budget ladder).
- **…__agent-…**: a sub-agent (Task tool) spawned by the named parent.

`gpt56-math` holds the four gpt-5.6-sol (Codex) red runs, a third builder model
outside the 40-run design. AuditBench and gpt-5.6-sol transcripts were converted to
the Claude Code shape by `transcript_adapters.py`; their original files are under
`raw/<round>/<run>/source/` (gpt-5.6-sol reasoning is provider-encrypted and shows
as a placeholder).

`status` is the run's recorded outcome (`ORGANISM`, `REFUSED_AUP`,
`CONTAMINATED`, `TRAINED_UNVALIDATED`, …). `passed` marks the green-gated
organisms published to Hugging Face (`hf_repo`, `pass_metric`).

## The three formats

1. **Markdown (`markdown/`)**: the readable conversation. User and assistant turns,
   collapsible thinking, each tool call with its input and output, compaction
   markers, and abandoned (rewound) branches. Harness bookkeeping is omitted and
   tool outputs over {cap:,} characters are truncated (the JSON has them in full).
   Best viewed in VS Code's Markdown preview, Obsidian, or GitHub.
2. **Normalized JSON (`json/`)**: one pretty-printed object per transcript:
   `{{"agent", "id", "title", "meta", "events": [...], "export": {{…index entry…}}}}`.
   `events` is a flat, time-ordered list, each item keyed by `kind`:
   - `user` / `assistant` have `blocks` of type `text`, `thinking`,
     `tool_use` (with `name`, `input`, and the tool's `result` attached), or `image`.
   - `notice` is a system-injected message (e.g. a background-task notification).
   - `attachment` is harness context (re-attached files, reminders…).
   - `system` with `subtype: "compact_boundary"` marks a context compaction.
   - `branch` holds rewound / abandoned branches (`groups` of events).
   This is the cc_transcript_viewer event contract (`event_schema.py`).
3. **Raw (`raw/`)**: the original Claude Code session JSONL (one JSON record per line),
   byte for byte, plus `status.txt`, sub-agent folders, and offloaded
   `tool-results/`. Use it if you need a field the normalized form drops.

## Quick recipes

```bash
# all passed organisms
jq '.[] | select(.passed) | {{round, run, pass_metric, hf_repo}}' index.json
# every Bash command a given blue audit ran
jq -r '.events[] | select(.kind=="assistant") | .blocks[]
       | select(.type=="tool_use" and .name=="Bash") | .input.command' \\
   json/opus48-math/run9/blue-audit-1.json
```
```python
import json
idx = json.load(open("index.json"))
for row in idx:
    s = json.load(open(row["json"]))
    texts = [b["text"] for e in s["events"] if e["kind"] == "assistant"
             for b in e["blocks"] if b["type"] == "text"]
```

## Browsing in the viewer (optional)

`raw/` is exactly the tree the cc_transcript_viewer serves. With that repo:
```bash
python3 server.py --runs /path/to/{name}/raw   # → http://127.0.0.1:3132/
```

## Notes

- {n_files} transcripts across {n_runs} runs; {n_dups} exact-duplicate file(s) appear
  in `index.json` with `duplicate_of` and are not exported a second time.
- Agent system prompts are not recorded in Claude Code transcripts, so they are not here.
- Exported {date} with `export_runs.py` from cc_transcript_viewer.
"""


def build(runs_dir: Path, out_dir: Path, name: str) -> Path:
    bundle = out_dir / name
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)
    shutil.copytree(runs_dir, bundle / "raw")

    records = _discover(runs_dir)
    index: list[dict] = []
    for rec in records:
        rel = rec["path"].relative_to(runs_dir)
        rec["raw_path"] = f"raw/{rel.as_posix()}"
        stem = f"{rec['round']}/{rec['run']}/{rec['name']}"
        row = {k: v for k, v in rec.items() if k != "path"}
        if "duplicate_of" in rec:
            row["json"] = row["markdown"] = None
            index.append(row)
            continue
        row["json"], row["markdown"] = f"json/{stem}.json", f"markdown/{stem}.md"
        session = claude.parse_session(rec["path"])
        events = session.get("events", [])
        times = [e["ts"] for e in events if e.get("ts")]
        row.update({
            "title": session.get("title", ""),
            "cwd": session.get("meta", {}).get("cwd", ""),
            "started": min(times) if times else None,
            "ended": max(times) if times else None,
            "n_user": sum(e["kind"] == "user" for e in events),
            "n_assistant": sum(e["kind"] == "assistant" for e in events),
            "n_tool_calls": sum(b.get("type") == "tool_use" for e in events
                                for b in e.get("blocks") or []),
            "models": sorted({e["model"] for e in events if e.get("model")}),
        })
        session["export"] = row
        jpath, mpath = bundle / row["json"], bundle / row["markdown"]
        jpath.parent.mkdir(parents=True, exist_ok=True)
        mpath.parent.mkdir(parents=True, exist_ok=True)
        jpath.write_text(json.dumps(session, indent=2, ensure_ascii=False))
        mpath.write_text(_markdown(rec, session, row["json"]))
        index.append(row)

    (bundle / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False))
    cols = ["round", "run", "team", "role", "name", "primary", "status", "passed",
            "pass_metric", "hf_repo", "title", "started", "ended", "n_user",
            "n_assistant", "n_tool_calls", "models", "lost", "duplicate_of",
            "markdown", "json", "raw_path"]
    with open(bundle / "index.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for row in index:
            w.writerow({**row, "models": ";".join(row.get("models") or [])})

    round_rows = []
    for rnd in dict.fromkeys(r["round"] for r in index):
        rows = [r for r in index if r["round"] == rnd]
        order = ["red", "green", "blue", "auditbench"]
        teams = sorted({r["team"] for r in rows}, key=lambda t: order.index(t) if t in order else 99)
        round_rows.append(f"| `{rnd}` | {rows[0]['model']} | {rows[0]['domain']} | "
                          f"{len({r['run'] for r in rows})} | {', '.join(teams)} |")
    (bundle / "README.md").write_text(_README.format(
        date=dt.date.today().isoformat(), name=name, cap=_MD_RESULT_CAP,
        round_rows="\n".join(round_rows), n_files=sum(r["json"] is not None for r in index),
        n_runs=len({(r["round"], r["run"]) for r in index}),
        n_dups=sum("duplicate_of" in r for r in index)))

    zpath = out_dir / f"{name}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(bundle.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(out_dir).as_posix())
    return zpath


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", default="runs_mirror", help="runs_mirror dir to export")
    ap.add_argument("--out", default=".", help="where to write the bundle dir + zip")
    ap.add_argument("--name", default=f"auditing_game_transcripts_{dt.date.today():%Y-%m-%d}")
    a = ap.parse_args()
    z = build(Path(a.runs).expanduser().resolve(), Path(a.out).expanduser().resolve(), a.name)
    print(f"wrote {z} ({z.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
