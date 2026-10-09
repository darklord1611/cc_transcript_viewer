#!/usr/bin/env python3
"""Adapters: convert foreign agent-transcript formats into Claude Code JSONL.

The run view parses every transcript with claude_parser, so any other agent
format is normalized *once, at staging time* into the record shape Claude Code
writes (``{"type": "user"|"assistant"|"system", "uuid", "parentUuid",
"timestamp", "message": {"role", "content": [blocks]}}``), with tool calls as
``tool_use`` blocks and their outputs as ``tool_result`` blocks on the next user
record. The original file is kept next to the conversion (collect_runs.py puts
it under ``<run>/source/``), so nothing is lost.

Adding a format = one function returning a list of records, registered in
ADAPTERS. Stdlib only; reads local files only.

Formats:
- ``auditbench``  — AuditBench / docent ``transcript.json`` (system/user/assistant/
                    tool messages; assistant ``tool_calls`` with ``function`` +
                    ``arguments``; tool messages keyed by ``tool_call_id``).
- ``codex_thread`` — Codex app-server ``thread_items`` table of a
                    ``transcript_thread_history.sqlite`` (userMessage /
                    agentMessage / commandExecution / fileChange items). Richer
                    than the flattened ``transcript.jsonl`` export: it keeps
                    command output and file diffs. Encrypted reasoning
                    (``<think redacted="true">``) becomes a placeholder.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

_THINK = re.compile(r"<think\b([^>]*)>(.*?)</think>", re.S)


class _Builder:
    """Accumulates records as one linear parentUuid chain (no branches)."""

    def __init__(self, session_id: str, cwd: str = "", model: str = ""):
        self.records: list[dict] = []
        self.session_id = session_id
        self.cwd = cwd
        self.model = model
        self._last: str | None = None

    def _add(self, rtype: str, ts: str | None, **fields) -> dict:
        rid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.session_id}/{len(self.records)}"))
        rec = {"type": rtype, "uuid": rid, "parentUuid": self._last, "sessionId": self.session_id,
               "timestamp": ts, "isSidechain": False, **fields}
        if self.cwd:
            rec["cwd"] = self.cwd
        self.records.append(rec)
        self._last = rid
        return rec

    def system(self, text: str, ts: str | None, subtype: str = "system_prompt") -> None:
        self._add("system", ts, subtype=subtype, content=text)

    def user(self, blocks: list[dict], ts: str | None) -> None:
        if blocks:
            self._add("user", ts, message={"role": "user", "content": blocks})

    def assistant(self, blocks: list[dict], ts: str | None, model: str = "") -> None:
        if blocks:
            self._add("assistant", ts, message={"role": "assistant", "model": model or self.model,
                                                "content": blocks})

    def tool_result(self, tool_id: str, text: str, ts: str | None, is_error: bool = False) -> None:
        self.user([{"type": "tool_result", "tool_use_id": tool_id,
                    "content": text, "is_error": is_error}], ts)


def _iso_ms(ms) -> str | None:
    try:
        return (datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
                .isoformat(timespec="milliseconds").replace("+00:00", "Z"))
    except (TypeError, ValueError, OSError):
        return None


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
    return "" if content is None else json.dumps(content)


# ---------------------------------------------------------------- AuditBench

def auditbench(path: Path, *, start_ts: str | None = None, model: str = "") -> list[dict]:
    """AuditBench ``transcript.json`` → records. Messages carry no timestamps;
    ``start_ts`` (from experiment_metadata) stamps the session start."""
    data = json.loads(Path(path).read_text())
    b = _Builder(str(data.get("id") or Path(path).parent.name), model=model)
    for m in data.get("messages", []):
        role, content = m.get("role"), m.get("content")
        if role == "system":
            b.system(_text(content), start_ts)
        elif role == "user":
            b.user([{"type": "text", "text": _text(content)}], start_ts)
        elif role == "assistant":
            blocks = []
            text = _text(content).strip()
            if text:
                blocks.append({"type": "text", "text": text})
            for call in m.get("tool_calls") or []:
                blocks.append({"type": "tool_use", "id": call.get("id") or str(uuid.uuid4()),
                               "name": call.get("function") or "tool",
                               "input": call.get("arguments") if call.get("arguments") is not None else {}})
            b.assistant(blocks, start_ts, model=m.get("model") or "")
        elif role == "tool":
            err = m.get("error")
            body = _text(content)
            if err:
                body = (body + "\n" if body else "") + f"[error] {err if isinstance(err, str) else json.dumps(err)}"
            b.tool_result(m.get("tool_call_id") or "", body, start_ts, is_error=bool(err))
        start_ts = None  # only the first record carries the session start
    return b.records


# ---------------------------------------------------------------- Codex thread

def _agent_blocks(text: str) -> list[dict]:
    blocks = []
    for attrs, body in _THINK.findall(text or ""):
        if 'redacted="true"' in attrs:
            blocks.append({"type": "thinking", "thinking": "🔒 reasoning encrypted by the provider (not recoverable)"})
        elif body.strip():
            blocks.append({"type": "thinking", "thinking": body.strip()})
    visible = _THINK.sub("", text or "").strip()
    if visible:
        blocks.append({"type": "text", "text": visible})
    return blocks


def _patch(changes) -> str:
    parts = []
    for ch in changes or []:
        kind = (ch.get("kind") or {}).get("type", "update") if isinstance(ch.get("kind"), dict) else ch.get("kind")
        verb = {"add": "Add", "delete": "Delete"}.get(str(kind), "Update")
        parts.append(f"*** {verb} File: {ch.get('path', '')}\n{ch.get('diff', '')}".rstrip())
    return "\n".join(parts)


def codex_thread(path: Path, *, model: str = "") -> list[dict]:
    """Codex ``transcript_thread_history.sqlite`` (thread_items) → records."""
    con = sqlite3.connect(f"file:{Path(path)}?mode=ro", uri=True)
    try:
        rows = con.execute("SELECT thread_id, created_at_ms, item_json FROM thread_items "
                           "ORDER BY rollout_ordinal, created_at_ms").fetchall()
    finally:
        con.close()
    b = _Builder(rows[0][0] if rows else Path(path).stem, model=model)
    for _tid, ms, raw in rows:
        item, ts = json.loads(raw), _iso_ms(ms)
        t = item.get("type")
        if t == "userMessage":
            b.user([{"type": "text", "text": _text(item.get("content"))}], ts)
        elif t == "agentMessage":
            b.assistant(_agent_blocks(item.get("text", "")), ts)
        elif t == "commandExecution":
            if item.get("cwd") and not b.cwd:
                b.cwd = item["cwd"]
            tid = item.get("id") or str(uuid.uuid4())
            b.assistant([{"type": "tool_use", "id": tid, "name": "exec_command",
                          "input": {"cmd": item.get("command", ""), "workdir": item.get("cwd")}}], ts)
            out = item.get("aggregatedOutput") or ""
            code = item.get("exitCode")
            if code not in (0, None):
                out += f"\n[exit code {code}]"
            b.tool_result(tid, out, ts, is_error=code not in (0, None))
        elif t == "fileChange":
            tid = item.get("id") or str(uuid.uuid4())
            b.assistant([{"type": "tool_use", "id": tid, "name": "apply_patch",
                          "input": {"patch": _patch(item.get("changes"))}}], ts)
            b.tool_result(tid, item.get("status") or "applied", ts)
        else:  # unknown item: keep it visible rather than drop it
            b.assistant([{"type": "text", "text": f"[{t}]\n```json\n{json.dumps(item, indent=2)[:4000]}\n```"}], ts)
    return b.records


ADAPTERS = {"auditbench": auditbench, "codex_thread": codex_thread}


def convert(fmt: str, src: Path, dest: Path, **kwargs) -> int:
    """Convert ``src`` with adapter ``fmt`` and write Claude Code JSONL to ``dest``."""
    records = ADAPTERS[fmt](src, **kwargs)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return len(records)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Convert a transcript to Claude Code JSONL.")
    ap.add_argument("format", choices=sorted(ADAPTERS))
    ap.add_argument("src", type=Path)
    ap.add_argument("dest", type=Path)
    a = ap.parse_args()
    print(f"wrote {convert(a.format, a.src, a.dest)} records → {a.dest}")
