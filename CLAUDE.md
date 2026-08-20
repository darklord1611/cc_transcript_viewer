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
python3 -m unittest test_security test_summary_cache test_parsers test_event_schema test_mirror test_auth  # full suite
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

Two decoupled processes let one hub oversee many RunPod pods' transcripts:

- **`collector.py`** (the *only* networked component) reads a `pods.json` registry
  and pulls each pod's transcript dirs over SSH into
  `<mirror>/<pod-id>/{claude,codex}/…`, then writes `<pod-id>/pod.json` with the
  team/game labels. Transport is `ssh 'tar -c' | tar -x` into a staging dir (tar
  needs nothing installed on the pod and preserves mtimes, so the summary cache
  stays valid) followed by a *local* `rsync -a --delete` into the live mirror (the
  hub has rsync) for per-file-atomic updates + deletion propagation. Run
  `--once` or `--interval N`. `pods.json` and `mirror/` are gitignored (infra
  IPs/keys); `pods.example.json` is the template.
- **`server.py --mirror <dir>`** serves that tree and never touches the network,
  so its "no outbound connections" guarantee is intact — all reach-out is in the
  collector, which `server.py` does not import.

`mirror.py` is the viewer-side glue: it points the existing parsers at each pod's
roots in turn and tags every session with `pod`/`team`/`game`; local mode is
untouched. Two invariants it upholds: it never clears the parsers' summary caches
(it sets the module globals directly, since paths are pod-unique), and it
serializes those global swaps under a lock because the server is threaded. In
mirror mode the allowed-root check (`resolve_transcript_file`) confines reads to
the mirrored pod trees via `mirror.owns()`. `test_mirror.py` covers tagging and
confinement.

### Run-structured view (auditing game, keyed by run number)

`python3 server.py --runs <dir>` renders one **nested tree per run number** instead
of a flat/pod list: each run's red organism is the parent row, with its 5 blue
audits + green eval nested beneath (reusing the existing sub-agent nesting —
`is_subagent`/`parent_file`). This matches how the three teams' durable transcripts
are organized on their separate `/workspace` volumes (see
`AutoSandbag/plans/ARTIFACTS_INDEX.md`): the run number is the unifying key.

- **`collect_runs.py`** stages the transcripts into `runs_mirror/run<N>/{red,blue,green}/`
  over SSH. Blue is auto-discovered (already numbered by run on the audits volume);
  red/green are mapped run→slug via `RED_MAP`/`GREEN_MAP` (from `MAPPING.md` /
  `GREEN_TRANSCRIPTS.md`). A run whose red transcript was never harvested (red3/red5
  pods shut down first) gets a `_LOST.jsonl` placeholder carrying the substitute note,
  so the gap shows in place (`red_lost` flag → LOST badge).
- **`runs.py`** is the viewer glue: enumerate `run<N>/` dirs (plus named controls like
  `overt`, sorted after the numbered runs), parse each transcript with claude_parser,
  and tag `run`/`team`/`role`, linking blue/green as children of the red parent.
  `server.list_sessions` keeps run mode's parent-then-children order (no mtime regroup);
  reads are confined to `runs_mirror` via `runs.owns()`. Frontend adds a **Run** filter
  and run/role/LOST badges. `test_runs.py` covers nesting, tagging, order, confinement.

Run mode takes precedence over `--mirror`/local when set. `runs_mirror/` is gitignored.

### Auth (public-endpoint token gate)

`--auth-token <t>` / `CC_VIEWER_TOKEN` env turns on a shared-secret gate so the
viewer can be exposed on a public `--host`. When set, `Handler._guard()` (called
by every verb) requires the token via `Authorization: Bearer`, `X-Auth-Token`,
`?token=`, or the `cc_auth` cookie; the index page, visited once as
`…/?token=<t>`, sets that HttpOnly/SameSite=Strict cookie so the SPA's `/api/*`
fetches authenticate without the token in every URL. Comparison is
`hmac.compare_digest`. Auth is orthogonal to the loopback Host-header guard
(`HOST_CHECK`): both can apply. The server prints a warning if bound
non-loopback with no token, and the intended TLS story is RunPod's HTTPS proxy in
front (the app speaks plain HTTP behind it). `test_auth.py` covers the gate.

Known gap: images referenced by absolute pod paths don't resolve on the hub (the
file lives on the pod) — a collector addition for later.

## Fragility to be aware of

The parsers depend on the *current* on-disk transcript formats of Claude Code, Codex, and Cursor. If
any tool changes how it stores sessions, parsing can silently drop records until updated —
`cursor_binary.py` in particular is reverse-engineered from observed wire bytes. Custom overrides
live outside the agent-owned transcripts and Cursor DB, which are never modified. See "Transcript
format notes" in `README.md` for the exact record shapes each parser consumes.
