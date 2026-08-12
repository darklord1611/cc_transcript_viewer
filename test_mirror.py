#!/usr/bin/env python3
"""Mirror-mode (multi-pod oversight) tests: pod discovery, tagging, confinement."""
import json
import tempfile
import unittest
from pathlib import Path

import claude_parser as claude
import mirror
import server
from test_fixtures import _write_fixture_session


def _make_pod(mirror_dir: Path, pod_id: str, team: str, prompt: str) -> Path:
    pod = mirror_dir / pod_id
    projects = pod / "claude" / "projects"
    projects.mkdir(parents=True)
    (pod / "pod.json").write_text(json.dumps({"team": team, "game": "audit-1"}))
    # Unique session id per pod so mirror paths never collide.
    _write_fixture_session(
        projects,
        session_id=f"{pod_id}-1111-1111-1111-111111111111"[:36].ljust(36, "0"),
        prompt=prompt,
    )
    return pod


class MirrorModeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.mirror_dir = Path(self._tmp.name) / "mirror"
        self.mirror_dir.mkdir()
        _make_pod(self.mirror_dir, "pod-red", "red", "red team recon")
        _make_pod(self.mirror_dir, "pod-blue", "blue", "blue team defense")
        mirror.configure(self.mirror_dir)

    def tearDown(self):
        mirror.configure(None)
        claude.configure(claude.DEFAULT_PROJECTS_DIR)
        self._tmp.cleanup()

    def test_sessions_tagged_by_pod_and_team(self):
        sessions = server.list_sessions()
        teams = {s.get("team") for s in sessions}
        pods = {s.get("pod") for s in sessions}
        self.assertEqual(teams, {"red", "blue"})
        self.assertEqual(pods, {"pod-red", "pod-blue"})
        self.assertTrue(all(s.get("game") == "audit-1" for s in sessions))

    def test_session_loads_and_stays_tagged(self):
        sessions = server.list_sessions()
        red = next(s for s in sessions if s.get("team") == "red")
        data = server.load_session(red["file"])
        self.assertIsNotNone(data)
        self.assertEqual(data["team"], "red")
        self.assertEqual(data["pod"], "pod-red")

    def test_reads_confined_to_mirror(self):
        # A real transcript outside every pod tree must be rejected in mirror mode.
        with tempfile.TemporaryDirectory() as outside:
            stray = _write_fixture_session(Path(outside))
            self.assertIsNone(server.load_session(str(stray)))
            with self.assertRaises(PermissionError):
                server.resolve_transcript_file(str(stray))

    def test_staging_dotdir_is_not_a_pod(self):
        # The collector's .staging scratch area must never be listed as a pod.
        (self.mirror_dir / ".staging" / "junk").mkdir(parents=True)
        pods = {s.get("pod") for s in server.list_sessions()}
        self.assertEqual(pods, {"pod-red", "pod-blue"})

    def test_pod_without_metadata_still_lists(self):
        bare = self.mirror_dir / "pod-green"
        (bare / "claude" / "projects").mkdir(parents=True)
        _write_fixture_session(
            bare / "claude" / "projects",
            session_id="green000-2222-2222-2222-222222222222"[:36].ljust(36, "0"),
            prompt="green",
        )
        sessions = server.list_sessions()
        green = [s for s in sessions if s.get("pod") == "pod-green"]
        self.assertEqual(len(green), 1)
        self.assertNotIn("team", green[0])  # no pod.json → no team tag


if __name__ == "__main__":
    unittest.main()
