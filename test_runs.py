#!/usr/bin/env python3
"""Run-structured view tests: per-run nesting, role/team tagging, ordering, confinement."""
import json
import tempfile
import unittest
from pathlib import Path

import claude_parser as claude
import runs
import server


def _mk(path: Path, prompt: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    recs = [
        {"type": "user", "timestamp": "2024-01-01T00:00:00Z", "cwd": "/w",
         "message": {"role": "user", "content": prompt}},
        {"type": "assistant", "timestamp": "2024-01-01T00:00:01Z",
         "message": {"role": "assistant", "model": "claude-test",
                     "content": [{"type": "text", "text": "ok"}]}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")
    return path


class RunsModeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "runs_mirror"
        # run1: red LOST + 2 blue audits (one a _RESUME variant) + 1 green
        _mk(self.dir / "run1" / "red" / "_LOST.jsonl", "lost note")
        _mk(self.dir / "run1" / "blue" / "audit_runs_1" / "agent_transcript" / "a.jsonl", "blue a")
        _mk(self.dir / "run1" / "blue" / "audit_runs_1_RESUME" / "agent_transcript" / "b.jsonl", "resume")
        _mk(self.dir / "run1" / "green" / "slug" / "g.jsonl", "green eval")
        # run2: real red organism + 1 blue
        _mk(self.dir / "run2" / "red" / "org-slug" / "r.jsonl", "red organism")
        _mk(self.dir / "run2" / "blue" / "audit_runs_1" / "agent_transcript" / "c.jsonl", "blue c")
        # overt control (must sort last)
        _mk(self.dir / "overt" / "red" / "_LOST.jsonl", "overt lost")
        runs.configure(self.dir)

    def tearDown(self):
        runs.configure(None)
        claude.configure(claude.DEFAULT_PROJECTS_DIR)
        self._tmp.cleanup()

    def test_nesting_and_tags(self):
        out = server.list_sessions()
        by_run = {}
        for s in out:
            by_run.setdefault(s["run"], []).append(s)

        run1 = by_run["run1"]
        parent = run1[0]
        self.assertFalse(parent["is_subagent"])
        self.assertEqual(parent["team"], "red")
        self.assertEqual(parent["role"], "organism")
        self.assertTrue(parent["red_lost"])
        # children point at the red parent and carry team/role
        children = run1[1:]
        self.assertTrue(all(c["is_subagent"] and c["parent_file"] == parent["file"] for c in children))
        roles = [c["role"] for c in children]
        self.assertEqual(roles, ["audit 1", "audit 1 · resume", "eval"])
        self.assertEqual([c["team"] for c in children], ["blue", "blue", "green"])

    def test_real_red_is_not_lost(self):
        out = server.list_sessions()
        run2_parent = next(s for s in out if s["run"] == "run2" and not s["is_subagent"])
        self.assertNotIn("red_lost", run2_parent)

    def test_run_order_controls_last(self):
        order = [s["run"] for s in server.list_sessions() if not s["is_subagent"]]
        self.assertEqual(order, ["run1", "run2", "overt"])

    def test_reads_confined_to_runs_dir(self):
        with tempfile.TemporaryDirectory() as outside:
            stray = _mk(Path(outside) / "x.jsonl", "stray")
            self.assertIsNone(server.load_session(str(stray)))
            with self.assertRaises(PermissionError):
                server.resolve_transcript_file(str(stray))


if __name__ == "__main__":
    unittest.main()
