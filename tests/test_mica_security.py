#!/usr/bin/env python3
"""Security regressions for Mica's service account and transcript path access.

Uses temporary files and mocked account metadata; never needs sudo or changes
system accounts. Run with: python -m unittest tests.test_mica_security
"""

from __future__ import annotations

import grp
import json
import os
import plistlib
import pwd
import tempfile
import time
import unittest
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from mica import capture, install
from mica import store as v


def _line(obj) -> str:
    return json.dumps(obj) + "\n"


def _append(path: Path, *records) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(_line(rec))


class SymlinkSecurity(unittest.TestCase):
    """A temp Claude projects tree + Codex sessions tree, a store, and a
    capturer driven by a fake clock."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.projects = self.tmp / "home" / ".claude" / "projects"
        self.codex_sessions = self.tmp / "home" / ".codex" / "sessions"
        self.codex_archived = self.tmp / "home" / ".codex" / "archived_sessions"
        for d in (self.projects, self.codex_sessions, self.codex_archived):
            d.mkdir(parents=True)
        self.store_dir = self.tmp / "mica"
        # Real time: retention checks compare against the files' real mtimes.
        self.now = time.time()
        self.capturer = self._new_capturer()

    def tearDown(self):
        for dirpath, dirnames, _files in os.walk(self.tmp):
            for d in dirnames:
                try:
                    os.chmod(os.path.join(dirpath, d), 0o755)
                except OSError:
                    pass
        self._tmp.cleanup()

    def _new_capturer(self, **kw):
        return capture.Capturer(
            self.store_dir,
            capture.default_sources(self.tmp / "home"),
            clock=lambda: self.now,
            **kw,
        )

    def tick(self, n: int = 1, seconds: float = 2.0):
        for _ in range(n):
            self.now += seconds
            self.capturer.poll_once(self.now)

    @property
    def reader(self) -> v.StoreReader:
        return v.StoreReader(self.store_dir)

    def key(self, path: Path) -> str:
        key = self.reader.key_for_path(str(path))
        self.assertIsNotNone(key, f"{path} not tracked")
        return key

    def entry_for_name(self, name: str) -> tuple:
        matches = [(k, e) for k, e in self.reader.index().items() if e["path"].endswith(name)]
        self.assertEqual(len(matches), 1, matches)
        return matches[0]

    def session(self, name="s1", records=None) -> Path:
        proj = self.projects / "-tmp-proj"
        proj.mkdir(exist_ok=True)
        path = proj / f"{name}.jsonl"
        recs = records if records is not None else [{"n": 1}, {"n": 2}]
        path.write_text("".join(_line(r) for r in recs), encoding="utf-8")
        return path

    def events_of(self, key: str) -> list:
        return [e["type"] for e in self.reader.events(key)]


    def _redirect_directory(self, directory: Path, live: Path, content: str):
        saved = self.tmp / "saved-directory"
        fake = self.tmp / "fake-directory"
        directory.rename(saved)
        redirected_file = fake / live.relative_to(directory)
        redirected_file.parent.mkdir(parents=True)
        redirected_file.write_text(content)
        directory.symlink_to(fake, target_is_directory=True)
        return saved

    def _assert_redirect_was_not_captured(self, key: str, original: str):
        rec = self.reader.record(key)
        self.assertEqual(rec["status"], "active")
        self.assertEqual(len(rec["generations"]), 1)
        self.assertEqual(self.reader.generation_path(key, rec["generations"][0]).read_text(), original)
        self.assertIn("unreadable", self.events_of(key))
        self.assertEqual(capture.verify_store(self.store_dir), [])

    def test_tracked_project_redirect_cannot_append_fake_messages(self):
        path = self.session()
        original = path.read_text()
        self.capturer.start()
        key = self.key(path)
        self._redirect_directory(path.parent, path, original + _line({"fake": True}))
        self.tick(n=3)
        self._assert_redirect_was_not_captured(key, original)

    def test_source_root_redirect_is_rejected_and_capture_recovers(self):
        path = self.session()
        original = path.read_text()
        self.capturer.start()
        key = self.key(path)
        saved = self._redirect_directory(self.projects, path, original + _line({"fake": True}))
        self.tick(n=3)
        self._assert_redirect_was_not_captured(key, original)
        self.assertFalse(self.capturer.source_state["claude"]["ok"])
        self.assertIn("symlink", self.capturer.source_state["claude"]["error"])
        self.projects.unlink()
        saved.rename(self.projects)
        _append(path, {"real": True})
        self.tick()
        self.assertTrue(self.capturer.source_state["claude"]["ok"])
        rec = self.reader.record(key)
        self.assertEqual(rec["flags"], [])
        self.assertEqual(len(rec["generations"]), 1)
        self.assertEqual(self.reader.generation_path(key, rec["generations"][0]).read_text(), original + _line({"real": True}))
        global_events = [e["type"] for e in v.read_jsonl(self.store_dir / v.EVENTS_FILE)]
        self.assertIn("access_lost", global_events)
        self.assertIn("access_restored", global_events)

    def test_source_ancestor_redirect_cannot_replace_captured_history(self):
        path = self.session()
        original = path.read_text()
        self.capturer.start()
        key = self.key(path)
        self._redirect_directory(self.projects.parent, path, _line({"fake": True}))
        self.tick(n=3)
        self._assert_redirect_was_not_captured(key, original)
        self.assertFalse(self.capturer.source_state["claude"]["ok"])

    def test_restart_does_not_resolve_a_redirected_source_root(self):
        path = self.session()
        original = path.read_text()
        self.capturer.start()
        key = self.key(path)
        config = [src.to_json() for src in self.capturer.sources]
        self._redirect_directory(self.projects, path, original + _line({"fake": True}))
        self.capturer = capture.Capturer(
            self.store_dir, [capture.Source.from_json(src) for src in config], clock=lambda: self.now,
        )
        self.capturer.start()
        self._assert_redirect_was_not_captured(key, original)
        self.assertFalse(self.capturer.source_state["claude"]["ok"])
        self.assertEqual([src.to_json() for src in self.capturer.sources], config)

    def test_a_source_that_is_already_a_symlink_is_refused(self):
        path = self.session()
        self._redirect_directory(self.projects, path, _line({"fake": True}))
        self.capturer = self._new_capturer()
        self.capturer.start()
        self.assertEqual(self.reader.index(), {})
        self.assertFalse(self.capturer.source_state["claude"]["ok"])

    def test_missing_source_tree_is_still_allowed(self):
        self.codex_archived.rmdir()
        self.capturer.start()
        self.assertEqual(self.capturer.source_state["codex-archived"], {"ok": True, "error": "missing"})
        self.codex_archived.mkdir()
        later = self.codex_archived / "rollout-later.jsonl"
        later.write_text(_line({"real": True}))
        self.tick()
        self.assertIsNotNone(self.reader.key_for_path(str(later)))

    @unittest.skipIf(os.geteuid() == 0, "root ignores permissions")
    def test_search_only_ancestors_do_not_require_directory_read_access(self):
        path = self.session()
        home = self.tmp / "home"
        os.chmod(home, 0o100)
        try:
            with self.assertRaises(PermissionError):
                os.listdir(home)
            self.capturer.start()
            key = self.key(path)
            _append(path, {"real": True})
            self.tick()
            self.assertEqual(self.reader.compare(key, path)["state"], "verified")
            self.assertTrue(self.capturer.source_state["claude"]["ok"])
        finally:
            os.chmod(home, 0o755)

    def test_directory_swap_before_open_is_rejected(self):
        path = self.session()
        real_open = os.open
        swapped = False

        def swap_before_open(name, flags, *args, **kwargs):
            nonlocal swapped
            if name == path.parent.name and kwargs.get("dir_fd") is not None and not swapped:
                swapped = True
                self._redirect_directory(path.parent, path, _line({"fake": True}))
            return real_open(name, flags, *args, **kwargs)

        with mock.patch.object(capture.os, "open", side_effect=swap_before_open):
            with self.assertRaises(OSError):
                capture._open_regular(str(path))
        self.assertTrue(swapped)

    def test_directory_swap_after_open_keeps_the_original_directory_fd(self):
        path = self.session()
        original = path.read_bytes()
        real_open = os.open
        swapped = False

        def swap_after_open(name, flags, *args, **kwargs):
            nonlocal swapped
            fd = real_open(name, flags, *args, **kwargs)
            if name == path.parent.name and kwargs.get("dir_fd") is not None and not swapped:
                swapped = True
                self._redirect_directory(path.parent, path, _line({"fake": True}))
            return fd

        with mock.patch.object(capture.os, "open", side_effect=swap_after_open):
            fd, _st = capture._open_regular(str(path))
        with os.fdopen(fd, "rb") as fh:
            self.assertEqual(fh.read(), original)
        self.assertTrue(swapped)
        with self.assertRaises(OSError):
            capture._open_regular(str(path))

    def test_file_swap_between_stat_and_open_is_rejected(self):
        path = self.session()
        original = path.read_text()
        self.capturer.start()
        key = self.key(path)
        # Trigger processing; replace the file only after its metadata check.
        _append(path, {"real": True})
        fake = self.tmp / "fake.jsonl"
        fake.write_text(original + _line({"fake": True}))
        real_open = os.open
        swapped = False

        def swap_before_open(name, flags, *args, **kwargs):
            nonlocal swapped
            if name == path.name and kwargs.get("dir_fd") is not None and not swapped:
                swapped = True
                path.rename(self.tmp / "saved-file.jsonl")
                path.symlink_to(fake)
            return real_open(name, flags, *args, **kwargs)

        with mock.patch.object(capture.os, "open", side_effect=swap_before_open):
            self.tick()
        self.assertTrue(swapped)
        self._assert_redirect_was_not_captured(key, original)


class ExistingServiceAccount(unittest.TestCase):
    """Account preflight is read-only; no real system accounts are touched."""

    def setUp(self):
        self.account = pwd.struct_passwd(("_mica", "*", 321, 321, "", "/var/empty", "/usr/bin/false"))
        self.group = grp.struct_group(("_mica", "*", 321, []))
        self.owner = pwd.struct_passwd(("someone", "*", 501, 20, "", "/Users/someone", "/bin/zsh"))
        self.attrs = {f"dsAttrTypeStandard:{key}": [value] for key, value in {
            "UniqueID": "321", "PrimaryGroupID": "321", "UserShell": "/usr/bin/false",
            "NFSHomeDirectory": "/var/empty", "Password": "*",
        }.items()}
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.user_lookup = stack.enter_context(mock.patch.object(install.pwd, "getpwnam", return_value=self.account))
        self.group_lookup = stack.enter_context(mock.patch.object(install.grp, "getgrnam", return_value=self.group))
        self.users = stack.enter_context(mock.patch.object(install.pwd, "getpwall", return_value=[self.account, self.owner]))
        self.groups = stack.enter_context(mock.patch.object(install.grp, "getgrall", return_value=[self.group]))
        self.memberships = stack.enter_context(mock.patch.object(install.os, "getgrouplist", return_value=[321]))
        self.run = stack.enter_context(mock.patch.object(install.subprocess, "run"))
        self.run.return_value.stdout = plistlib.dumps(self.attrs)

    def test_valid_account_is_reused_without_mutations(self):
        self.assertTrue(install._service_user_exists(self.owner))
        self.run.assert_called_once_with(
            ["/usr/bin/dscl", "-plist", ".", "-read", "/Users/_mica"],
            check=True, capture_output=True,
        )

    def test_absent_account_and_group_allow_creation(self):
        self.user_lookup.side_effect = KeyError("_mica")
        self.group_lookup.side_effect = KeyError("_mica")
        self.assertFalse(install._service_user_exists(self.owner))
        self.run.assert_not_called()

    def test_orphaned_group_is_not_overwritten(self):
        self.user_lookup.side_effect = KeyError("_mica")
        with self.assertRaisesRegex(SystemExit, "refusing to overwrite existing group"):
            install._service_user_exists(self.owner)
        self.run.assert_not_called()

    def test_missing_service_group_is_rejected(self):
        self.group_lookup.side_effect = KeyError("_mica")
        with self.assertRaisesRegex(SystemExit, "group _mica is missing"):
            install._service_user_exists(self.owner)

    def test_unsafe_account_fields_are_rejected(self):
        for index, value, message in (
            (2, 0, "system-account range"), (2, 501, "shares the transcript owner's uid"),
            (3, 80, "dedicated uid/gid"), (5, "/Users/someone", "must have shell"),
            (6, "/bin/zsh", "must have shell"),
        ):
            with self.subTest(value=value):
                fields = list(self.account)
                fields[index] = value
                self.user_lookup.return_value = pwd.struct_passwd(fields)
                with self.assertRaisesRegex(SystemExit, message):
                    install._service_user_exists(self.owner)

    def test_shared_user_and_group_ids_are_rejected(self):
        for user, message in (
            (pwd.struct_passwd(("alias", "*", 321, 20, "", "/tmp", "/bin/zsh")), "shares its uid"),
            (pwd.struct_passwd(("alias", "*", 502, 321, "", "/tmp", "/bin/zsh")), "uses its primary group"),
        ):
            with self.subTest(message=message):
                self.users.return_value = [self.account, self.owner, user]
                with self.assertRaisesRegex(SystemExit, message):
                    install._service_user_exists(self.owner)
        self.users.return_value = [self.account, self.owner]
        self.groups.return_value = [self.group, grp.struct_group(("alias", "*", 321, []))]
        with self.assertRaisesRegex(SystemExit, "another group shares its gid"):
            install._service_user_exists(self.owner)

    def test_implicit_macos_memberships_are_accepted(self):
        # What macOS really reports for every local account (e.g. _www):
        # everyone, localaccounts, _lpoperator, com.apple.sharepoint.group.1.
        self.memberships.return_value = [321, 12, 61, 100, 701]
        self.assertTrue(install._service_user_exists(self.owner))

    def test_other_group_members_and_supplementary_groups_are_rejected(self):
        self.group_lookup.return_value = grp.struct_group(("_mica", "*", 321, ["someone"]))
        with self.assertRaisesRegex(SystemExit, "other users are members"):
            install._service_user_exists(self.owner)
        self.group_lookup.return_value = self.group
        self.memberships.return_value = [321, 12, 80]
        with self.assertRaisesRegex(SystemExit, "administrator group"):
            install._service_user_exists(self.owner)
        self.memberships.return_value = [321]
        self.groups.return_value = [self.group, grp.struct_group(("staff", "*", 20, ["_mica"]))]
        with self.assertRaisesRegex(SystemExit, "belongs to groups besides"):
            install._service_user_exists(self.owner)

    def test_enabled_authentication_or_inconsistent_local_metadata_is_rejected(self):
        for key, value in (
            ("Password", "enabled"), ("AuthenticationAuthority", ";ShadowHash;"),
            ("UniqueID", "501"),
        ):
            with self.subTest(key=key):
                attrs = dict(self.attrs, **{f"dsAttrTypeStandard:{key}": [value]})
                self.run.return_value.stdout = plistlib.dumps(attrs)
                with self.assertRaisesRegex(SystemExit, "refusing to reuse"):
                    install._service_user_exists(self.owner)

    def test_metadata_or_membership_lookup_failure_aborts(self):
        self.run.return_value.stdout = b"Operation failed with error: eServerError"
        with self.assertRaisesRegex(SystemExit, "could not verify"):
            install._service_user_exists(self.owner)
        self.run.return_value.stdout = plistlib.dumps(self.attrs)
        self.memberships.side_effect = OSError("lookup failed")
        with self.assertRaisesRegex(SystemExit, "could not verify"):
            install._service_user_exists(self.owner)

    def test_invalid_account_aborts_before_confirmation_or_plan_creation(self):
        self.memberships.return_value = [321, 80]
        args = mock.Mock(user="someone", dry_run=True)
        with (
            mock.patch.object(install, "_require_macos_root"),
            mock.patch.object(install, "_target_user", return_value=self.owner),
            mock.patch.object(install, "build_install_plan") as plan,
            mock.patch.object(install, "_confirm") as confirm,
            redirect_stdout(StringIO()),
        ):
            with self.assertRaisesRegex(SystemExit, "refusing to reuse"):
                install.cmd_install(args)
        plan.assert_not_called()
        confirm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
