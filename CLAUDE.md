# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A zero-dependency local web app that browses Claude Code, Codex, and Cursor transcripts in one
time-sorted view. Read-only, loopback-only, no outbound network code. `README.md` documents the
user-facing behavior; this file covers the internals.

## Hard constraints

These are load-bearing, not stylistic. Breaking any of them breaks the project's core claim (see
`prompt_request.md`, the build-it-yourself spec handed to distrustful users):

- **Python 3.9+ standard library only.** No `pip install`, no frameworks. Backend network code is
  `http.server` (inbound) and nothing else.
- **No outbound network code anywhere, ever.** `test_security.py` statically asserts that no module
  imports a network client and dynamically asserts that no request opens a non-loopback socket.
- **No frontend build step.** Vanilla JS/CSS/HTML served as static files. `marked`/`DOMPurify`/`KaTeX`
  load from a CDN *in the browser* and degrade to plain text offline.

## Commands

```bash
python3 server.py                      # run (http://127.0.0.1:3132/)
python3 -m unittest test_security test_summary_cache        # full suite (~1s)
python3 -m unittest test_security.SecurityTest.test_foreign_host_header_rejected   # one test
```

There is no linter, formatter, or build config. Tests are hermetic: `setUpClass` writes JSONL/SQLite
fixtures into a temp dir, points the modules at them via `server.PROJECTS_DIR = …` /
`codex.configure(…)` / `cursor.configure(…)`, and serves on an ephemeral loopback port.

## Architecture

`server.py` is the only entry point. `codex_server.py` and `cursor_server.py` are parsing libraries it
imports; both hold their roots in module-level globals mutated by a `configure()` call at startup, so
anything that reaches them (including tests) must `configure()` first.

### Two event shapes, one renderer

Every parser emits a list of events, but in two different shapes, and `static/app.js:renderEvent`
dispatches on `ev.kind` across both:

- **Block shape** (`kind: user|assistant`, with a `blocks: [...]` list of `text`/`thinking`/`tool_use`)
  — produced by Claude Code *and by Cursor*. `cursor_server.py` deliberately normalizes Cursor's tool
  names and inputs onto the Claude Code renderers via `_TOOL_NAME_MAP`, so a Cursor `edit_file` shows
  the same colorized diff as a Claude `Edit`. **Adding Cursor tool support means adding a mapping,
  not writing a new renderer.**
- **Flat shape** (`kind: tool|reasoning|web_search|guardian_request|…`, fields directly on the event)
  — produced by Codex.

Anything that walks events generically must handle both. `server.py:_event_text` (search indexing) is
the reference for how, and is the thing that most often needs updating when a parser gains a field.

### Session addressing and read confinement

Sessions are addressed by filesystem path, *except* Cursor, which lives in a SQLite DB and uses the
synthetic `cursordb:<composerId>` scheme. `load_session(file_id)` is the single dispatch point for
both, and `parse_session()` enforces root confinement — a path outside `~/.claude/projects`,
`~/.codex/sessions`, or `~/.codex/archived_sessions` returns `None`, which the handler turns into a
`403`. **Any new endpoint that reads a session must go through `load_session`**, never `Path(...)`
directly. (`/api/local-image` is the deliberate exception: transcripts reference images by their
original path anywhere on disk, so it is constrained by MIME type — image only — rather than by root.)

### Caching layers (all keyed by mtime)

Four independent caches keep the 1-second live poll from re-reading every transcript:

- `_SUMMARY_CACHE` in `server.py` plus its counterpart in `codex_server.py` — sidebar summaries keyed
  by `(st_mtime_ns, st_size)`, persisted to `~/.cache/transcript_viewer/summaries.json`. **Bump
  `_CACHE_VERSION` whenever the summary dict's shape changes**, or stale entries load from disk and
  the sidebar silently shows old fields. A cold scan of ≥ `_PARALLEL_SCAN_THRESHOLD` stale files fans
  out over a `ProcessPoolExecutor`.
- `_TEXT_CACHE` — the searchable (user text, everything else) split per session.
- `_AGENT_CALLS_CACHE` — a parent session's `Task`/`Agent` calls, used to label its sub-agents.
- `_CUSTOM_NAMES_CACHE` — the viewer-owned names file.

### Claude Code specifics

- **The conversation is a tree, not a list.** Records carry `uuid`/`parentUuid`; editing or rewinding
  a message forks it, and every branch is appended to the same file, so a flat read interleaves
  abandoned turns. `_fold_branches` walks root→active-leaf (the latest `last-prompt` record, then down
  to the real tip) and folds abandoned sibling subtrees into inline `branch` events. It has a safety
  net: if the reconstructed path wouldn't cover every event, it returns the flat list unchanged rather
  than risk dropping content. Preserve that property.
- **Not every `user` record is a user prompt.** Claude Code records background-task notifications,
  slash-command machinery, hook output, and system reminders as `user` records wrapped in a
  recognizable opening tag. `_SYNTHETIC_USER_LABELS` is the single place they're classified, so the
  title, the `n_user` count, and the rendered outline all agree. To support a new wrapper, add its tag
  there and nowhere else.
- **Sub-agents are separate transcripts.** Newer versions write them to
  `<project>/<session-id>/subagents/agent-*.jsonl`. They're listed as their own entries but re-sorted
  to sit directly under their parent in `list_sessions()`. Older transcripts inline them as
  `isSidechain` records instead; both paths still render.

### Custom names

Viewer-specific titles live in `~/.config/cc_transcript_viewer/names.json` — never in the agent-owned
transcripts or the Cursor DB. The key (`_custom_name_key`) is `agent:[parent:]id` rather than a path,
so a name survives the transcript moving on disk (notably Codex archival). Writes are atomic
same-directory replacements.

## Security invariants (pinned by test_security.py)

Changing any of these means changing a test, which should make you stop and think:

- Default bind is `127.0.0.1`. A **`Host`-header allowlist** (`HOST_CHECK`) guards every route against
  DNS rebinding, and is disabled *only* when the user deliberately binds a non-loopback `--host`.
  `--allowed-host` widens the allowlist by exact hostname (for a trusted reverse proxy that forwards
  the browser's `Host` to the loopback socket) **without** turning the guard off; `ALLOWED_HOSTS` is
  empty unless the user names something. Prefer extending it over disabling `HOST_CHECK`.
- `/api/session` is confined to the transcript roots; `/api/local-image` serves image-typed files only;
  `/api/session-name` accepts JSON only and writes only the configured names file.
- The frontend renders untrusted transcript content — keep everything going through `esc()` or
  DOMPurify.

## A note on format drift

The parsers depend on the *current* on-disk formats of three third-party tools. When a transcript
renders wrong, suspect a format change before suspecting the renderer, and verify against a real file
under `~/.claude/projects`, `~/.codex/sessions`, or Cursor's `state.vscdb` rather than reasoning from
the shapes documented here.
