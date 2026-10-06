"""The viewer checks recording settings without changing or disclosing config."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import settings_check


class RecordingSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.claude = Path(self.tmp.name) / "claude"
        self.codex = Path(self.tmp.name) / "codex"
        self.claude.mkdir()
        self.codex.mkdir()

    def check(self):
        return settings_check.check_settings(self.claude, self.codex)["issues"]

    def configure(self, days=99999, thinking=True, codex='model_reasoning_summary = "detailed"\n'):
        (self.claude / "settings.json").write_text(json.dumps({
            "cleanupPeriodDays": days, "showThinkingSummaries": thinking, "secret": "DO_NOT_DISCLOSE",
        }))
        (self.codex / "config.toml").write_text(codex)

    def test_missing_and_recommended_settings(self):
        self.assertEqual(len(self.check()), 3)
        self.configure()
        self.assertEqual(self.check(), [])
        self.configure(days=100000)
        self.assertEqual(self.check(), [])
        self.configure(days=10000)
        self.assertEqual(self.check(), [])
        self.configure(days=9999)
        self.assertEqual([i["setting"] for i in self.check()], ["cleanupPeriodDays"])

    def test_changes_refresh_checks_without_exposing_other_settings(self):
        self.configure()
        self.assertEqual(self.check(), [])
        self.configure(days=30, thinking=False, codex='model_reasoning_summary = "none"\napi_key = "DO_NOT_DISCLOSE"\n')
        issues = self.check()
        self.assertEqual(len(issues), 3)
        self.assertNotIn("DO_NOT_DISCLOSE", json.dumps(issues))
        self.configure(days=True)
        self.assertEqual([i["setting"] for i in self.check()], ["cleanupPeriodDays"])

    def test_invalid_and_unreadable_settings_warn(self):
        self.configure()
        (self.claude / "settings.json").write_text("{invalid")
        self.assertEqual(len(self.check()), 2)
        self.configure()
        settings_check._CACHE.clear()
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            self.assertEqual(len(self.check()), 3)

    def test_absent_agents_do_not_warn(self):
        self.claude.rmdir()
        self.codex.rmdir()
        self.assertEqual(self.check(), [])

    def test_tables_comments_and_active_profiles_on_old_python(self):
        for parser in (settings_check.tomllib, None):
            with self.subTest(parser=parser), mock.patch.object(settings_check, "tomllib", parser):
                settings_check._CACHE.clear()
                self.configure(codex='# model_reasoning_summary = "detailed"\n[profiles.other]\nmodel_reasoning_summary = "detailed"\n')
                self.assertEqual([i["setting"] for i in self.check()], ["model_reasoning_summary"])
                self.configure(codex="model_reasoning_summary = 'detailed' # comment\nprofile = 'work'\n[profiles.work]\nmodel_reasoning_summary = 'none'\n")
                self.assertEqual([i["setting"] for i in self.check()], ["profiles.work.model_reasoning_summary"])
                self.configure(codex="model_reasoning_summary = 'detailed'\n[profiles.work]\nmodel_reasoning_summary = 'none'\n")
                self.assertEqual(self.check(), [])

    def test_unchanged_settings_stay_cached(self):
        self.configure()
        self.assertEqual(self.check(), [])
        with mock.patch.object(Path, "read_text", side_effect=AssertionError("should not read unchanged config")):
            self.assertEqual(self.check(), [])


if __name__ == "__main__":
    unittest.main()
