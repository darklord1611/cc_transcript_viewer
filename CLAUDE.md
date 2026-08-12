# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A zero-dependency, standard-library-only local web app that browses coding-agent transcripts —
Claude Code, Codex, and Cursor — in one time-sorted view. It is read-only over the network, binds
loopback by default, and has **no outbound-network code at all** (a security invariant, not just a
default — see below). Python 3.9+, no `pip install`, no build step. `README.md` is the canonical
user-facing reference and documents every feature and on-disk transcript format in detail.

## Commands

```bash
python3 server.py                      # run the app (http://127.0.0.1:3132/)
python3 server.py --port 8080          # alternate port
python3 server.py --host 0.0.0.0       # LAN exposure (disables the Host-header guard)
python3 server.py --projects-dir PATH  # override Claude Code projects dir
python3 server.py --codex-home PATH    # override Codex home (default ~/.codex)
python3 server.py --cursor-db PATH     # override Cursor state.vscdb (or its app-support dir)

# Tests (stdlib unittest only; no linter/formatter config and no CI in the repo)
python3 -m unittest test_security test_summary_cache test_parsers test_event_schema test_mirror  # full suite
python3 -m unittest test_parsers                                                     # one module
python3 -m unittest test_security.SecurityTest.test_runtime_makes_no_outbound_connections  # one test
```

`test_fixtures.py` is a shared fixture-builder imported by the test modules, not a suite itself.

## Architecture

**Server is orchestration; parsing lives in three parser modules that all emit one event shape.**
`server.py` no longer parses transcripts — it is the stdlib `http.server` handler plus session
listing, search, custom names, the security guards, and the file-open endpoints. It imports the
three parsers and dispatches to them:

- `claude_parser.py` — Claude Code JSONL (`~/.claude/projects/…`), including sub-agent files and
  branch folding.
- `codex_parser.py` — Codex rollout JSONL + `state_5.sqlite` metadata; unpacks orchestration-style
  `exec` calls and renders `apply_patch` diffs.
- `cursor_parser.py` — Cursor's `state.vscdb` read-only, addressed by the `cursordb:<id>` scheme;
  reconstructs `edit_file` diffs from `composer.content.*` snapshots.

Each parser exposes the same surface: `configure(path)`, `list_sessions()`, `session_summary(path)`,
`parse_session(path)`. `server.py:load_session` picks the parser by which transcript root the id
falls under (or the `cursordb:` scheme).

Two supporting modules:

- `common.py` — shared helpers: `iter_jsonl`, `short_title`, `iso_from_ms`, `file_identity`, and the
  `SummaryCache` class (the mtime-keyed cache used by every parser).
- `cursor_binary.py` — decodes Cursor's `toolFormerData.toolCallBinary` protobuf blobs by hand (wire
  format only, no `.proto`, no dependency). Newer Cursor versions store grep/glob/await tool results
  here instead of in the JSON `result` field, so this is required to recover those outputs.

**The event contract is the central abstraction — `event_schema.py`.** Every parser emits a session
as `{"agent", "id", "title", "meta", "events", …}` where `events` is a flat list of dicts each keyed
by `kind` (`user`/`assistant`/`reasoning`/`tool`/`branch`/`notice`/`attachment`/`instructions`/
`system`/`guardian_*`/`web_*`/`raw`/…). `event_schema.py` is the single written-down description of
what each kind carries plus `validate_event`/`validate_summary`/`validate_session`. The frontend's
`renderEvent()` must have a case for every kind in `KINDS`. **When you add or change a `kind`, update
all three of: the emitting parser, `event_schema.py`, and `app.js`** — `test_event_schema.py` checks
`app.js` against `KINDS`, and `test_parsers.py` validates real parser output against the schema, so
drift fails the suite.

Note the two message shapes the contract allows: **block shape** (Claude Code + Cursor: a `blocks`
list, tool calls ride inside the assistant turn with results attached) and **flat shape** (Codex:
plain `text`, with reasoning/tool calls as separate top-level events).

**Frontend** — `static/{index.html,style.css,app.js}`, vanilla JS, no build. `app.js` dispatches on
`ev.kind`, renders both shapes, runs the live-refresh poll loop (sidebar + open transcript update in
place without losing scroll or expand/collapse state), and persists the theme. Markdown/math/
sanitization (`marked`, `DOMPurify`, `KaTeX`) load from a CDN and degrade to plain text offline.

**API** (`server.py:Handler`): `GET /api/sessions`, `GET /api/session?file=…`,
`GET /api/session-state`, `GET /api/search?q=…`, `GET /api/local-image?path=…`,
`PUT /api/session-name`, `POST /api/open-local`, `POST /api/reveal-transcript`, plus the static files.

### Performance model: the mtime-keyed summary cache

Sidebar cost must not grow with accumulated transcript count. Per-file summaries are cached by
`(mtime, size)` file identity (Codex also keys on a thread-metadata signature) via `common.SummaryCache`,
persisted to disk, and re-read only when a file changes. When touching summary/listing code, do not
read full transcript bodies during listing and keep the invalidation key correct — this is what
`test_summary_cache.py` protects.

### Security invariants (do not break)

Asserted by `test_security.py`; these are load-bearing product claims, not incidental:

- **No network-client imports and no runtime outbound connections** in any module. Do not add
  `requests`, `urllib.request`, socket clients, mail, or telemetry. The test installs a socket-level
  guard and greps imports. (`subprocess` *is* imported — only to open/reveal local files, macOS-only;
  it never touches the network.)
- **Loopback bind by default** with a `Host`-header allowlist (`127.0.0.1`/`localhost`) guarding
  against DNS rebinding; skipped only when a non-loopback `--host` is passed (`HOST_CHECK`).
- **Confined file access.** `/api/session` parses only files under the allowed transcript roots (or a
  `cursordb:` id); `/api/local-image` serves only image-typed files; `/api/session-name` writes only
  the viewer-owned names file (`~/.config/cc_transcript_viewer/names.json`) via atomic replace after
  verifying the transcript exists. `open_local_file`/`reveal_transcript_file` confine opens to the
  session workspace, reject executables, and are macOS-only.

### Mirror mode (multi-pod oversight)

`python3 server.py --mirror <dir>` switches the viewer from the local machine's
transcripts to per-pod transcript trees under `<dir>/<pod-id>/{claude,codex}/…`
(populated out-of-band by a collector that rsyncs each RunPod pod). All of this
lives in `mirror.py`, which points the existing parsers at each pod's roots in
turn and tags every session with `pod`/`team`/`game`; local mode is untouched.
Two invariants it upholds: it never clears the parsers' summary caches (it sets
the module globals directly, since paths are pod-unique), and it serializes those
global swaps under a lock because the server is threaded. In mirror mode the
allowed-root check (`resolve_transcript_file`) confines reads to the mirrored pod
trees via `mirror.owns()`. `test_mirror.py` covers tagging and confinement.

## Fragility to be aware of

The parsers depend on the *current* on-disk transcript formats of Claude Code, Codex, and Cursor. If
any tool changes how it stores sessions, parsing can silently drop records until updated —
`cursor_binary.py` in particular is reverse-engineered from observed wire bytes. Custom overrides
live outside the agent-owned transcripts and Cursor DB, which are never modified. See "Transcript
format notes" in `README.md` for the exact record shapes each parser consumes.
