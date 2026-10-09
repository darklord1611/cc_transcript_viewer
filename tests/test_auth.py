#!/usr/bin/env python3
"""Auth-gate tests: with a token set, every route requires it; the tokened
index visit hands back a cookie that subsequent requests can use."""
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import claude_parser as claude
import server
from tests.fixture_builders import _write_fixture_session

TOKEN = "s3cr3t-token"


class AuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls._tmp.name)
        cls.projects_dir = tmp / "projects"
        cls.projects_dir.mkdir()
        cls.fixture = _write_fixture_session(cls.projects_dir)
        claude.configure(cls.projects_dir)
        server.CUSTOM_NAMES_FILE = tmp / "viewer" / "names.json"
        cls._old_cache = server.CACHE_FILE
        server.CACHE_FILE = tmp / "cache" / "summaries.json"
        # Token on, but keep the loopback Host guard off so these tests isolate
        # auth (Host-header behaviour is covered in test_security).
        cls._old_host_check = server.HOST_CHECK
        server.HOST_CHECK = False
        server.AUTH_TOKEN = TOKEN

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        server.AUTH_TOKEN = None
        server.HOST_CHECK = cls._old_host_check
        server.CACHE_FILE = cls._old_cache
        cls._tmp.cleanup()

    def req(self, path: str, headers: dict | None = None):
        url = f"http://127.0.0.1:{self.port}{path}"
        r = urllib.request.Request(url, headers=headers or {})
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_no_token_is_rejected(self):
        status, _, _ = self.req("/api/sessions")
        self.assertEqual(status, 401)

    def test_wrong_token_is_rejected(self):
        status, _, _ = self.req("/api/sessions", {"Authorization": "Bearer nope"})
        self.assertEqual(status, 401)

    def test_bearer_header_is_accepted(self):
        status, _, body = self.req("/api/sessions", {"Authorization": f"Bearer {TOKEN}"})
        self.assertEqual(status, 200)
        self.assertIn(b"sessions", body)

    def test_x_auth_token_header_is_accepted(self):
        status, _, _ = self.req("/api/sessions", {"X-Auth-Token": TOKEN})
        self.assertEqual(status, 200)

    def test_query_param_is_accepted(self):
        status, _, _ = self.req(f"/api/sessions?token={TOKEN}")
        self.assertEqual(status, 200)

    def test_index_with_token_sets_cookie_that_then_authenticates(self):
        status, headers, _ = self.req(f"/?token={TOKEN}")
        self.assertEqual(status, 200)
        cookie = headers.get("Set-Cookie", "")
        self.assertIn(f"{server.AUTH_COOKIE}={TOKEN}", cookie)
        self.assertIn("HttpOnly", cookie)
        # The cookie alone (no query/header) now authenticates an API call.
        status, _, _ = self.req(
            "/api/sessions", {"Cookie": f"{server.AUTH_COOKIE}={TOKEN}"}
        )
        self.assertEqual(status, 200)

    def test_index_without_token_sets_no_cookie(self):
        # A wrong/absent token can't even reach the page, let alone get a cookie.
        status, headers, _ = self.req("/")
        self.assertEqual(status, 401)
        self.assertNotIn("Set-Cookie", headers)


if __name__ == "__main__":
    unittest.main()
