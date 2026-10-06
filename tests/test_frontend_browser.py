"""Optional browser journeys through the live and standalone frontends."""

from __future__ import annotations

import html as html_module
import json
import re
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import unquote

import codex_parser as codex
import export_html


FIXTURE = next(
    (Path(__file__).parent / "fixtures" / "transcripts" / "codex").glob("*.jsonl")
)
CHROME_CANDIDATES = (
    shutil.which("google-chrome"),
    shutil.which("chromium"),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
)
CHROME = next((path for path in CHROME_CANDIDATES if path and Path(path).is_file()), None)


@unittest.skipUnless(CHROME, "headless Chrome/Chromium is not installed")
class StandaloneBrowserJourneyTests(unittest.TestCase):
    def test_export_renders_and_copies_without_server_features(self):
        data = codex.parse_session(FIXTURE)
        rendered = export_html.build_standalone_html(
            data, exported_at="2026-01-02T03:04:11+00:00"
        )

        # The browser test is deliberately offline: remove CDN assets and
        # replace fetch before app.js starts. Markdown then exercises the
        # frontend's built-in plain-text fallback.
        rendered = re.sub(
            r'<script\b[^>]*\bsrc="https://[^"]+"[^>]*></script>', "", rendered
        )
        rendered = re.sub(r'<link\b[^>]*\bhref="https://[^"]+"[^>]*>', "", rendered)
        app_marker = '  <script>\n"use strict";'
        rendered = rendered.replace(
            app_marker,
            "  <script>window.__testFetchCalls = 0; "
            "window.fetch = () => { window.__testFetchCalls++; "
            "return Promise.reject(new Error('offline test')); };</script>\n"
            + app_marker,
            1,
        )
        probe = """<script>
          document.body.setAttribute("data-test-probe", encodeURIComponent(JSON.stringify({
            standalone: document.body.classList.contains("standalone"),
            sidebarDisplay: getComputedStyle(document.querySelector("#sidebar")).display,
            transcriptHidden: document.querySelector("#transcript").hidden,
            userTurns: document.querySelectorAll(".turn-user").length,
            assistantTurns: document.querySelectorAll(".turn-assistant").length,
            reasoningTurns: document.querySelectorAll(".turn-reasoning").length,
            toolTurns: document.querySelectorAll(".turn-tool").length,
            serverControls: document.querySelectorAll(".rename-btn, .reveal-btn").length +
              [...document.querySelectorAll(".t-controls button")]
                .filter((button) => button.textContent === "Save HTML").length,
            fetchCalls: window.__testFetchCalls,
            copy: transcriptToText(window.__TRANSCRIPT_EXPORT__),
          })));
        </script>"""
        rendered = rendered.replace("</body>", probe + "\n</body>", 1)

        probe_data = self.probe_page(rendered)
        self.assertEqual(
            {key: probe_data[key] for key in (
                "standalone", "sidebarDisplay", "transcriptHidden",
                "userTurns", "assistantTurns", "reasoningTurns", "toolTurns",
                "serverControls", "fetchCalls",
            )},
            {
                "standalone": True,
                "sidebarDisplay": "none",
                "transcriptHidden": False,
                "userTurns": 1,
                "assistantTurns": 1,
                "reasoningTurns": 1,
                "toolTurns": 1,
                "serverControls": 0,
                "fetchCalls": 0,
            },
        )
        self.assertIn("Summarize the example file.", probe_data["copy"])
        self.assertIn("/workspace/example-project/example.txt", probe_data["copy"])
        self.assertNotIn("TOOL_OUTPUT_SHOULD_NOT_COPY", probe_data["copy"])


    def test_setup_dismissal_and_persistent_access_warning(self):
        rendered = (export_html.STATIC_DIR / "index.html").read_text()
        rendered = re.sub(r'<script\b[^>]*\bsrc="https://[^"]+"[^>]*></script>', "", rendered)
        rendered = re.sub(r'<link\b[^>]*\bhref="https://[^"]+"[^>]*>', "", rendered)
        rendered = rendered.replace(export_html.STYLE_LINK,
                                    "<style>" + (export_html.STATIC_DIR / "style.css").read_text() + "</style>")
        mocked_api = """<script>
          localStorage.clear();
          window.__setup = {issues: [{agent: 'Claude Code', setting: 'cleanupPeriodDays',
            recommended: 10000, path: '/fixture/settings.json', reason: 'Keep transcripts.'}]};
          window.fetch = async () => ({json: async () => ({sessions: [], setup: window.__setup,
            mica: {enabled: true, running: true, n_files: 1,
              sources: {claude: {ok: false, error: 'permission denied'}}}})});
        </script>"""
        rendered = rendered.replace(export_html.APP_SCRIPT, mocked_api + "<script>\n" +
                                    (export_html.STATIC_DIR / "app.js").read_text() + "\n</script>")
        probe = """<script>
          setTimeout(() => {
            const banner = document.querySelector('#setup-banner');
            const result = {initialVisible: !banner.hidden,
              layoutFits: document.querySelector('#app').getBoundingClientRect().bottom <= innerHeight + 1};
            banner.querySelector('button').click();
            result.dismissed = banner.hidden;
            result.dismissalSaved = !!localStorage.getItem(SETUP_DISMISS_KEY);
            renderSetupBanner(window.__setup);
            result.staysDismissed = banner.hidden;
            const next = {issues: [...window.__setup.issues, {agent: 'Claude Code',
              setting: 'showThinkingSummaries', recommended: true, path: '/fixture/settings.json', reason: 'Save thinking.'}]};
            renderSetupBanner(next);
            result.newIssueVisible = !banner.hidden;
            banner.querySelector('button').click();
            renderSetupBanner({issues: []});
            renderSetupBanner(next);
            result.regressionVisible = !banner.hidden;
            result.sourceFailureVisible = document.querySelector('#mica-status').textContent.includes('capture incomplete');
            const box = el('div'), lock = el('span');
            fillMicaBanner(box, lock, {state: 'verified', flags: ['unreadable'],
              events: [{type: 'unreadable', t: '2026-01-01T00:00:00Z'}]}, {});
            result.gapWarningVisible = !box.hidden && box.textContent.includes('lost read access');
            result.cleanLockHidden = lock.hidden;
            result.sessionFlagged = isMicaFlagged({mica: {state: 'active', flags: ['unreadable']}});
            document.body.setAttribute('data-test-probe', encodeURIComponent(JSON.stringify(result)));
          }, 0);
        </script>"""
        rendered = rendered.replace("</body>", probe + "\n</body>", 1)
        result = self.probe_page(rendered)
        self.assertEqual(result, dict.fromkeys((
            "initialVisible", "layoutFits", "dismissed", "dismissalSaved", "staysDismissed",
            "newIssueVisible", "regressionVisible", "sourceFailureVisible", "gapWarningVisible",
            "cleanLockHidden", "sessionFlagged",
        ), True))

    def probe_page(self, rendered):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            page = tmp / "transcript.html"
            page.write_text(rendered, encoding="utf-8")
            # Chrome helper processes can inherit PIPE file descriptors after
            # the main process exits. Real files avoid waiting for those
            # unrelated descriptors to close.
            with tempfile.TemporaryFile(mode="w+") as stdout, tempfile.TemporaryFile(
                mode="w+"
            ) as stderr:
                process = subprocess.Popen(
                    [
                        CHROME,
                        "--headless",
                        "--disable-background-networking",
                        "--disable-component-update",
                        "--disable-default-apps",
                        "--disable-gpu",
                        "--disable-sync",
                        "--metrics-recording-only",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--virtual-time-budget=1000",
                        "--user-data-dir=" + str(tmp / "chrome-profile"),
                        "--dump-dom",
                        page.as_uri(),
                    ],
                    stdout=stdout,
                    stderr=stderr,
                    text=True,
                )
                try:
                    deadline = time.monotonic() + 10
                    rendered_dom = ""
                    while time.monotonic() < deadline:
                        stdout.seek(0)
                        rendered_dom = stdout.read()
                        if "data-test-probe=" in rendered_dom or process.poll() is not None:
                            break
                        time.sleep(0.05)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait(timeout=3)
                    stderr.seek(0)
                    browser_errors = stderr.read()

        match = re.search(r'data-test-probe="([^"]+)"', rendered_dom)
        self.assertIsNotNone(match, browser_errors[-2000:])
        return json.loads(unquote(html_module.unescape(match.group(1))))


if __name__ == "__main__":
    unittest.main()
