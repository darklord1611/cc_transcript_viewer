"""Focused tests for cross-parser session-list behavior in server.py."""

from __future__ import annotations

import json
import unittest
from unittest import mock

import claude_parser as claude
import server
from tests.fixture_builders import ViewerServerTestCase


class _FixtureParser:
    def __init__(self, sessions):
        self.sessions = sessions

    def list_sessions(self):
        return [dict(session) for session in self.sessions]


class SessionOrderingTests(unittest.TestCase):
    def test_last_activity_beats_file_mtime_and_subagents_stay_with_parent(self):
        sessions = [
            {
                "file": "old",
                "title": "old session with recently touched bookkeeping",
                "last_ts": "2026-01-01T00:00:00Z",
                "mtime": 9_999_999_999,
            },
            {
                "file": "new",
                "title": "new session",
                "last_ts": "2026-02-01T00:00:00Z",
                "mtime": 1,
            },
            {
                "file": "child",
                "title": "new session child",
                "last_ts": "2025-01-01T00:00:00Z",
                "mtime": 0,
                "is_subagent": True,
                "parent_file": "new",
            },
        ]
        with (
            mock.patch.object(server, "PARSERS", {"fixture": _FixtureParser(sessions)}),
            mock.patch.object(server, "load_summary_caches"),
            mock.patch.object(server, "save_summary_caches"),
            mock.patch.object(server, "_apply_custom_name"),
        ):
            ordered = server.list_sessions()

        self.assertEqual([session["file"] for session in ordered], ["new", "child", "old"])

    def test_invalid_or_missing_activity_falls_back_to_mtime(self):
        self.assertEqual(server._recency({"last_ts": "not-a-date", "mtime": 42}), 42)
        self.assertEqual(server._recency({"mtime": 17}), 17)


class CustomNameAndSearchTests(ViewerServerTestCase):
    def test_custom_name_can_be_set_and_cleared(self):
        payload = {"file": str(self.fixture), "name": "My custom transcript"}
        status, _, body = self.put_json("/api/session-name", payload)
        self.assertEqual(status, 200)
        saved = json.loads(body)
        self.assertEqual(saved["title"], "My custom transcript")
        self.assertEqual(saved["custom_title"], "My custom transcript")
        self.assertEqual(saved["original_title"], "hello world")

        _, _, body = self.get("/api/sessions")
        summary = next(s for s in json.loads(body)["sessions"] if s["file"] == str(self.fixture))
        self.assertEqual(summary["title"], "My custom transcript")

        status, _, body = self.put_json(
            "/api/session-name", {"file": str(self.fixture), "name": ""}
        )
        self.assertEqual(status, 200)
        restored = json.loads(body)
        self.assertEqual(restored["title"], "hello world")
        self.assertEqual(restored["custom_title"], "")

    def test_custom_title_search_outranks_user_message(self):
        data = server.load_session(str(self.fixture))
        server._set_custom_name(data, "priorityword custom name")
        try:
            matches = server.search_sessions("priorityword")
            self.assertEqual(matches[0]["file"], str(self.fixture))
            user_message_match = next(
                match for match in matches if match["file"] == str(self.priority_fixture)
            )
            self.assertGreater(matches[0]["score"], user_message_match["score"])
            self.assertGreaterEqual(matches[0]["score"], server.CUSTOM_TITLE_WEIGHT)
        finally:
            server._set_custom_name(data, "")

    def test_later_user_message_receives_user_weight(self):
        matches = server.search_sessions("laterpromptword")
        match = next(
            item for item in matches if item["file"] == str(self.later_prompt_fixture)
        )
        self.assertEqual(match["score"], server.USER_MSG_WEIGHT)

    def test_claude_native_metadata_is_exposed(self):
        summary = claude.session_summary(self.metadata_fixture)
        self.assertEqual(summary["title"], "nativepriority title")
        self.assertEqual(summary["claude_title"], "nativepriority title")
        self.assertEqual(summary["agent_name"], "reviewer")

        data = server.load_session(str(self.metadata_fixture))
        self.assertEqual(data["title"], "nativepriority title")
        self.assertEqual(data["meta"]["pr"]["number"], 42)
        compact = next(ev for ev in data["events"] if ev.get("subtype") == "compact_boundary")
        self.assertEqual(compact["compaction"]["pre_tokens"], 12000)
        self.assertEqual(compact["compaction"]["post_tokens"], 3500)
        self.assertEqual(compact["compaction"]["preserved_messages"], 2)
        self.assertEqual(compact["compaction"]["discovered_tools"], 2)

        match = next(
            item for item in server.search_sessions("nativepriority")
            if item["file"] == str(self.metadata_fixture)
        )
        self.assertEqual(match["score"], server.NATIVE_TITLE_WEIGHT)


class SearchTitleSegmentTests(unittest.TestCase):
    def test_agent_native_titles_have_half_custom_weight(self):
        claude = {
            "agent": "claude",
            "custom_title": "",
            "original_title": "Original Claude title",
            "claude_title": "Original Claude title",
            "ai_title": "Short Claude title",
        }
        cursor = {
            "agent": "cursor",
            "custom_title": "",
            "original_title": "Short Cursor title",
        }
        codex = {
            "agent": "codex",
            "custom_title": "",
            "original_title": "A long Codex prompt",
            "ai_title": "Short Codex title",
        }
        custom, native = server._search_title_segments(claude)
        self.assertEqual(custom, "")
        self.assertEqual(native.count("Original Claude title"), 1)
        self.assertIn("Short Claude title", native)
        self.assertEqual(
            server._search_title_segments(cursor),
            ("", "Short Cursor title"),
        )
        self.assertEqual(
            server._search_title_segments(codex),
            ("", "A long Codex prompt\nShort Codex title"),
        )


if __name__ == "__main__":
    unittest.main()
