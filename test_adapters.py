#!/usr/bin/env python3
"""transcript_adapters: foreign formats → Claude Code JSONL that claude_parser and the
event contract accept; plus run-mode nesting of the white-box + AuditBench children."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import claude_parser as claude
import event_schema
import runs
import transcript_adapters as ta


def _tools(session: dict) -> list[dict]:
    return [b for e in session["events"] for b in e.get("blocks") or [] if b["type"] == "tool_use"]


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_auditbench_transcript(self):
        src = self.dir / "transcript.json"
        src.write_text(json.dumps({"id": "abc", "messages": [
            {"role": "system", "content": "You are an auditing agent."},
            {"role": "user", "content": "Find the quirk."},
            {"role": "assistant", "content": "Reading the log.", "tool_calls": [
                {"id": "t1", "function": "Read", "arguments": {"file_path": "/x/log.md"}}]},
            {"role": "tool", "content": "1\tlog body", "tool_call_id": "t1", "function": "Read"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "t2", "function": "mcp__auditing__sample", "arguments": {"prompt": "hi"}}]},
            {"role": "tool", "content": "", "tool_call_id": "t2", "error": "timeout"},
            {"role": "assistant", "content": "Done."},
        ]}))
        out = self.dir / "ab.jsonl"
        ta.convert("auditbench", src, out, start_ts="2026-08-31T17:23:56Z", model="m")
        s = claude.parse_session(out)
        self.assertEqual(event_schema.validate_session(s), [])
        tools = _tools(s)
        self.assertEqual([t["name"] for t in tools], ["Read", "mcp__auditing__sample"])
        self.assertEqual(tools[0]["result"]["text"], "1\tlog body")
        self.assertTrue(tools[1]["result"]["is_error"])
        self.assertIn("timeout", tools[1]["result"]["text"])
        self.assertEqual(claude.session_summary(out)["title"][:15], "Find the quirk.")

    def test_codex_thread_sqlite(self):
        db = self.dir / "t.sqlite"
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE thread_items (thread_id TEXT, turn_id TEXT, item_id TEXT, "
                    "rollout_ordinal INTEGER, created_at_ms INTEGER, item_json TEXT, item_type TEXT)")
        items = [
            {"type": "userMessage", "content": [{"type": "text", "text": "Build the organism."}]},
            {"type": "agentMessage", "text": '<think signature="s" redacted="true">gAAAA</think>'},
            {"type": "commandExecution", "id": "e1", "command": "ls", "cwd": "/root/run",
             "aggregatedOutput": "a.txt\n", "exitCode": 0},
            {"type": "commandExecution", "id": "e2", "command": "false", "cwd": "/root/run",
             "aggregatedOutput": "", "exitCode": 1},
            {"type": "fileChange", "id": "f1", "status": "completed", "changes": [
                {"path": "/root/run/m.json", "kind": {"type": "update"}, "diff": "@@ -1 +1 @@\n-a\n+b"}]},
            {"type": "agentMessage", "text": "All done."},
        ]
        for i, it in enumerate(items):
            con.execute("INSERT INTO thread_items VALUES (?,?,?,?,?,?,?)",
                        ("th", "tu", str(i), i, 1788325095000 + i, json.dumps(it), it["type"]))
        con.commit()
        con.close()
        out = self.dir / "gpt.jsonl"
        ta.convert("codex_thread", db, out, model="gpt-x")
        s = claude.parse_session(out)
        self.assertEqual(event_schema.validate_session(s), [])
        tools = _tools(s)
        self.assertEqual([t["name"] for t in tools], ["exec_command", "exec_command", "apply_patch"])
        self.assertEqual(tools[0]["result"]["text"], "a.txt\n")
        self.assertTrue(tools[1]["result"]["is_error"])
        self.assertIn("*** Update File: /root/run/m.json", tools[2]["input"]["patch"])
        thinking = [b["text"] for e in s["events"] for b in e.get("blocks") or [] if b["type"] == "thinking"]
        self.assertTrue(thinking and "encrypted" in thinking[0])
        self.assertEqual(s["meta"]["cwd"], "/root/run")


class RunsNestingTest(unittest.TestCase):
    """blue_wb/ and auditbench/ children nest under the red organism, in order,
    with the right team; source/ (original foreign files) is never listed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name) / "runs_mirror"
        run = root / "opus48-math" / "run5"

        def mk(path, prompt):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"type": "user", "timestamp": "2024-01-01T00:00:00Z",
                                        "message": {"role": "user", "content": prompt}}) + "\n")

        mk(run / "red" / "slug" / "r.jsonl", "red")
        mk(run / "green" / "slug" / "g.jsonl", "green")
        mk(run / "blue" / "audit_runs_1" / "agent_transcript" / "b.jsonl", "bb")
        mk(run / "blue_wb" / "wb_audit_runs_4" / "agent_transcript" / "w.jsonl", "wb")
        mk(run / "auditbench" / "ab_150k_runs_1" / "l.jsonl", "ladder")
        mk(run / "auditbench" / "ab_audit_runs_2_B2" / "transcript.jsonl", "ab b2")
        mk(run / "auditbench" / "ab_audit_runs_2" / "transcript.jsonl", "ab")
        mk(run / "source" / "auditbench" / "x" / "stray.jsonl", "must not list")
        runs.configure(root)

    def tearDown(self):
        runs.configure(None)
        self._tmp.cleanup()

    def test_red_ignores_subagent_logs(self):
        run = Path(runs.RUNS_DIR) / "sonnet5-math" / "run29"
        main = run / "red" / "run29" / "abc.jsonl"
        sub = run / "red" / "run29" / "abc" / "subagents" / "agent-x.jsonl"
        for f in (main, sub):
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "x"}}) + "\n")
        self.assertEqual(runs._red_path(run), main)

    def test_order_and_teams(self):
        out = runs.collect()
        self.assertEqual([(s["team"], s["role"]) for s in out], [
            ("red", "organism"), ("green", "eval"), ("blue", "audit 1"), ("blue", "wb audit 4"),
            ("auditbench", "AB audit 2"), ("auditbench", "AB audit 2 · B2"),
            ("auditbench", "AB 150k audit 1"),
        ])
        parent = out[0]["file"]
        self.assertTrue(all(s["parent_file"] == parent for s in out[1:]))
        self.assertFalse(any("source" in Path(s["file"]).parts for s in out))


if __name__ == "__main__":
    unittest.main()
