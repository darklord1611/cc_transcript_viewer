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
        op = self.dir / "opus48-math"
        # run1: red LOST + 2 blue audits (one a _RESUME variant) + 1 green
        _mk(op / "run1" / "red" / "_LOST.jsonl", "lost note")
        _mk(op / "run1" / "blue" / "audit_runs_1" / "agent_transcript" / "a.jsonl", "blue a")
        _mk(op / "run1" / "blue" / "audit_runs_1_RESUME" / "agent_transcript" / "b.jsonl", "resume")
        _mk(op / "run1" / "green" / "slug" / "g.jsonl", "green eval")
        # run2: real red organism + 1 blue
        _mk(op / "run2" / "red" / "org-slug" / "r.jsonl", "red organism")
        _mk(op / "run2" / "blue" / "audit_runs_1" / "agent_transcript" / "c.jsonl", "blue c")
        # overt control (must sort last within the round)
        _mk(op / "overt" / "red" / "_LOST.jsonl", "overt lost")
        # A newer combo (sonnet5-code): red-only, status-badged; sorts BEFORE opus.
        s5 = self.dir / "sonnet5-code"
        _mk(s5 / "run7" / "red" / "slug" / "s.jsonl", "sonnet red")
        (s5 / "run7" / "status.txt").write_text("REFUSED_AUP")
        # A contaminated run, and a contaminated-but-refused one (NOT flagged).
        # Numbers chosen not to collide with opus48-math's run1/run2/overt.
        _mk(s5 / "run5" / "red" / "slug" / "c.jsonl", "contaminated")
        (s5 / "run5" / "status.txt").write_text("INVALID_CONTAMINATED")
        _mk(s5 / "run8" / "red" / "slug" / "cr.jsonl", "contam+refused")
        (s5 / "run8" / "status.txt").write_text("INVALID_CONTAMINATED_REFUSED")
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
        self.assertEqual(roles, ["eval", "audit 1", "audit 1 · resume"])
        self.assertEqual([c["team"] for c in children], ["green", "blue", "blue"])

    def test_real_red_is_not_lost(self):
        out = server.list_sessions()
        run2_parent = next(s for s in out if s["run"] == "run2" and not s["is_subagent"])
        self.assertNotIn("red_lost", run2_parent)

    def test_round_model_domain_and_order(self):
        out = server.list_sessions()
        # sonnet-5 combos (newer) sort before opus-4.8's.
        parents = [s for s in out if not s["is_subagent"]]
        self.assertEqual(parents[0]["round"], "sonnet-5 · code")
        self.assertEqual((parents[0]["round_model"], parents[0]["domain"]), ("sonnet-5", "code"))
        run1 = next(s for s in parents if s["run"] == "run1")
        self.assertEqual((run1["round_model"], run1["domain"]), ("opus-4.8", "math"))

    def test_passed_organism_marked(self):
        out = server.list_sessions()
        # opus48-math run1 is a real green-gated pass in passed_organisms.json.
        run1 = next(s for s in out if s["round"] == "opus-4.8 · math" and s["run"] == "run1")
        self.assertTrue(run1.get("passed"))
        self.assertIn("gap", run1.get("pass_metric", ""))
        self.assertTrue(run1.get("hf_repo"))
        # a run not in the pass list carries no marker
        s5 = next(s for s in out if s["round_model"] == "sonnet-5" and s["run"] == "run7")
        self.assertNotIn("passed", s5)

    def test_contaminated_flag_excludes_refused(self):
        out = server.list_sessions()
        by = {(s["round"], s["run"]): s for s in out if not s["is_subagent"]}
        # CONTAM (not refused) -> flagged; CONTAM+REFUSED -> not flagged.
        self.assertTrue(by[("sonnet-5 · code", "run5")].get("contaminated"))
        self.assertNotIn("contaminated", by[("sonnet-5 · code", "run8")])
        self.assertNotIn("contaminated", by[("sonnet-5 · code", "run7")])

    def test_sonnet_status_badge(self):
        s5 = next(s for s in server.list_sessions()
                  if s["round_model"] == "sonnet-5" and s["run"] == "run7")
        self.assertEqual(s5["status"], "REFUSED_AUP")

    def test_run_order_controls_last(self):
        opus = [s["run"] for s in server.list_sessions()
                if not s["is_subagent"] and s["round"] == "opus-4.8 · math"]
        self.assertEqual(opus, ["run1", "run2", "overt"])

    def test_reads_confined_to_runs_dir(self):
        with tempfile.TemporaryDirectory() as outside:
            stray = _mk(Path(outside) / "x.jsonl", "stray")
            self.assertIsNone(server.load_session(str(stray)))
            with self.assertRaises(PermissionError):
                server.resolve_transcript_file(str(stray))


if __name__ == "__main__":
    unittest.main()
