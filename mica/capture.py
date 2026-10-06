"""The capture engine: poll transcript folders and copy every append into the store.

Polling, not file-change notifications: the standard library has no FSEvents
binding, and polling keeps the daemon dependency-free. It stays cheap by
tiering the work:

  every poll (~1 s)   stat each known directory (a changed mtime means a file
                      was added, removed, or renamed there) and each file
                      that changed recently
  every cold poll     stat every tracked file, so edits to old transcripts
                      are noticed too
  every rescan        walk the source trees from scratch

For each changed file the engine asks one question: is the live file still an
extension of what was captured? If so, it copies the new bytes onto the end of
the current generation. If not (the file shrank, earlier bytes changed, or a
different file now sits at the path), it closes the generation, records a
tamper event, and starts a new generation from the file's current contents.
Captured bytes are never rewritten or removed.

Cheap checks run on every change (size, and the bytes just before the
captured end); a full byte-for-byte comparison of the captured prefix runs
when the file's identity changes and at most every ``verify_seconds`` for a
file that keeps changing. An edit deep inside a file can therefore be
*reported* up to that long after it happens, but the original bytes are
already in the store either way.
"""

from __future__ import annotations

import errno
import json
import os
import secrets
import stat as stat_mod
import time
from pathlib import Path

from mica import store as v

DEFAULT_POLL_SECONDS = 1.0
DEFAULT_COLD_SECONDS = 15.0
DEFAULT_RESCAN_SECONDS = 300.0
DEFAULT_VERIFY_SECONDS = 60.0
HEARTBEAT_SECONDS = 5.0
HOT_SECONDS = 300.0        # files changed this recently are statted every poll
TAIL_WINDOW = 64 * 1024    # bytes before the captured end checked on every change
COPY_CHUNK = 1 << 20

# Parsers the store knows how to select transcript files for.
PARSERS = ("claude", "codex")


def _source_path(path) -> Path:
    """Normalize system aliases, without resolving user-controlled symlinks.

    Root-level aliases such as macOS /var -> /private/var are controlled by
    the system. Everything below them must be opened without following links.
    In particular, loading a saved source must not silently redirect its root.
    """
    path = Path(os.path.abspath(os.path.expanduser(str(path))))
    if len(path.parts) > 1:
        system_dir = Path(os.path.realpath(Path(path.anchor) / path.parts[1]))
        path = system_dir.joinpath(*path.parts[2:])
    return path


class Source:
    """One transcript tree to capture: a name (used as the mirror directory
    in the store), the viewer parser that reads it, and its root."""

    def __init__(self, name: str, parser: str, root):
        if parser not in PARSERS:
            raise ValueError(f"unknown parser {parser!r}")
        if not v.valid_key(name):
            raise ValueError(f"invalid source name {name!r}")
        self.name = name
        self.parser = parser
        self.root = _source_path(root)

    def to_json(self) -> dict:
        return {"name": self.name, "parser": self.parser, "root": str(self.root)}

    @classmethod
    def from_json(cls, data: dict) -> "Source":
        return cls(data["name"], data["parser"], data["root"])

    def wants(self, rel_parts: tuple) -> bool:
        """Is this path (relative to the root) a transcript file?"""
        if not rel_parts or not rel_parts[-1].endswith(".jsonl"):
            return False
        if self.parser == "claude":
            # <project>/<session>.jsonl, or
            # <project>/<session>/subagents/agent-<id>.jsonl
            return len(rel_parts) == 2 or (len(rel_parts) == 4 and rel_parts[2] == "subagents")
        return rel_parts[-1].startswith("rollout-")


def default_sources(home) -> list:
    home = Path(home).expanduser()
    return [
        Source("claude", "claude", home / ".claude" / "projects"),
        Source("codex", "codex", home / ".codex" / "sessions"),
        Source("codex-archived", "codex", home / ".codex" / "archived_sessions"),
    ]


def _open_directory(path, *, readable: bool = False) -> int:
    """Open every component relative to its parent's fd, rejecting symlinks.

    Holding directory descriptors prevents a rename/symlink swap between a
    path check and an open from redirecting the next operation elsewhere.
    """
    path = _source_path(path)
    # Ancestors carry only the installer's search ACL, not list/read access.
    # O_SEARCH (macOS) / O_PATH (Linux) preserves that minimal permission.
    search = getattr(os, "O_SEARCH", getattr(os, "O_PATH", os.O_RDONLY))
    flags = search | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, flags, dir_fd=fd)
            except OSError as exc:
                # Some platforms report ENOTDIR rather than ELOOP for
                # O_DIRECTORY | O_NOFOLLOW. Treat this as lost access,
                # rather than pretending the transcript was deleted.
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise OSError(errno.ELOOP, "symlink or non-directory in transcript path", str(path)) from exc
                raise
            os.close(fd)
            fd = child
        if readable:
            child = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _stat_nofollow(path):
    path = _source_path(path)
    parent = _open_directory(path.parent)
    try:
        st = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if stat_mod.S_ISLNK(st.st_mode):
            raise OSError(errno.ELOOP, "symlink transcript rejected", str(path))
        return st
    finally:
        os.close(parent)


def _walk_directory(path):
    """Discover files from a safely opened root; fwalk rejects child links."""
    try:
        root = _open_directory(path, readable=True)
    except OSError:
        return  # Match os.walk: inaccessible or vanished trees yield nothing.
    try:
        for relative, dirs, files, _fd in os.fwalk(".", dir_fd=root, follow_symlinks=False):
            yield os.path.normpath(os.path.join(str(path), relative)), dirs, files
    finally:
        os.close(root)


def _open_regular(path: str):
    """Open a regular transcript without following any symlink component."""
    path = _source_path(path)
    parent = _open_directory(path.parent)
    try:
        # NONBLOCK also prevents a concurrent regular-file -> FIFO swap
        # from hanging capture before fstat can reject the replacement.
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
    finally:
        os.close(parent)
    try:
        st = os.fstat(fd)
        if not stat_mod.S_ISREG(st.st_mode):
            raise OSError(errno.EINVAL, "not a regular file", path)
        return fd, st
    except BaseException:
        os.close(fd)
        raise


def _sig(st) -> list:
    """Change signature of a file. ctime is included because, unlike mtime,
    it can't be set from user space: an edit that keeps the size and restores
    the old mtime still changes it."""
    return [st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino, st.st_dev]


def _dir_sig(st) -> tuple:
    """A directory's mtime changes when entries are added or removed; ctime
    too, and ctime can't be reset afterwards, so hiding a new file by
    restoring the directory's mtime doesn't delay its discovery."""
    return (st.st_mtime_ns, st.st_ctime_ns)


def _exists(path: str):
    """True/False when we can tell whether ``path`` exists; None when a
    permission error hides the answer (lost access is not a deletion)."""
    try:
        _stat_nofollow(path)
        return True
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            return False
        return None


class Capturer:
    def __init__(
        self,
        store_root,
        sources: list,
        *,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        cold_seconds: float = DEFAULT_COLD_SECONDS,
        rescan_seconds: float = DEFAULT_RESCAN_SECONDS,
        verify_seconds: float = DEFAULT_VERIFY_SECONDS,
        clock=time.time,
        log=None,
    ):
        self.root = Path(store_root).expanduser()
        self.files_dir = self.root / v.FILES_DIR
        self.sources = list(sources)
        self.poll_seconds = poll_seconds
        self.cold_seconds = cold_seconds
        self.rescan_seconds = rescan_seconds
        self.verify_seconds = verify_seconds
        self.clock = clock
        self.log = log or (lambda msg: None)

        self.records: dict = {}        # key -> persisted record dict
        self.runtime: dict = {}        # key -> in-memory bookkeeping
        self.by_path: dict = {}        # live path -> key of the active record
        self.by_inode: dict = {}       # (dev, ino) -> key of the active record
        self.files_by_dir: dict = {}   # dir -> set of tracked live paths in it
        self.deleted_by_path: dict = {}  # path -> key of the last deleted record there
        self.dir_mtimes: dict = {}     # dir path -> ((mtime_ns, ctime_ns), source)
        self.pending_missing: dict = {}  # key -> first time its file was seen missing
        self.source_state: dict = {}   # source name -> {"ok": bool, "error": str}
        self.unreadable_paths: set = set()
        self._index_dirty = False
        self._last_cold = 0.0
        self._last_rescan = 0.0
        self._last_heartbeat = 0.0
        self.started_at = None

    # ------------------------------------------------------------------ #
    # Startup
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        now = self.clock()
        self.started_at = now
        self.root.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(exist_ok=True)
        fmt = self.root / v.FORMAT_FILE
        if not fmt.exists():
            fmt.write_text(v.FORMAT_TEXT, encoding="utf-8")

        previous = v.read_json(self.root / v.STATUS_FILE)
        last_poll = v.parse_iso(previous.get("last_poll")) if isinstance(previous, dict) else None
        self._event(None, "daemon_started", {"pid": os.getpid()})
        if last_poll is not None and now - last_poll > max(3 * self.poll_seconds, HEARTBEAT_SECONDS * 2):
            self._event(None, "capture_gap", {"from": v.iso_utc(last_poll), "to": v.iso_utc(now)})

        self._check_sources(now)
        self._load_records()
        first_run = not self.records
        # Files that changed while the daemon was down get a full comparison.
        for key, rec in list(self.records.items()):
            if rec.get("status") != "active":
                continue
            path = rec["path"]
            try:
                st = _stat_nofollow(path)
            except OSError as exc:
                if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                    self.pending_missing.setdefault(key, now)
                else:
                    self._mark_unreadable(key, exc)
                continue
            if _sig(st) == rec.get("last_stat") and self._current(rec) is not None:
                self.runtime[key]["stat"] = _sig(st)
                continue
            self._process(key, path, st, now, full_verify=True, offline=True)
        self._full_scan(now, preexisting=first_run)
        self._resolve_missing(now, force=True)
        self._write_index()
        self._heartbeat(now, force=True)

    def _track(self, key: str) -> None:
        rec = self.records[key]
        path = rec["path"]
        self.by_path[path] = key
        self.by_inode[(rec.get("dev"), rec.get("ino"))] = key
        self.files_by_dir.setdefault(os.path.dirname(path), set()).add(path)

    def _untrack(self, key: str) -> None:
        rec = self.records[key]
        path = rec["path"]
        if self.by_path.get(path) == key:
            del self.by_path[path]
            self.files_by_dir.get(os.path.dirname(path), set()).discard(path)
        ident = (rec.get("dev"), rec.get("ino"))
        if self.by_inode.get(ident) == key:
            del self.by_inode[ident]

    def _load_records(self) -> None:
        if not self.files_dir.is_dir():
            return
        for entry in sorted(os.scandir(self.files_dir), key=lambda e: e.name):
            if not entry.is_dir(follow_symlinks=False) or not v.valid_key(entry.name):
                continue
            rec = v.read_json(Path(entry.path) / v.RECORD_FILE)
            if not isinstance(rec, dict) or rec.get("key") != entry.name:
                continue
            key = entry.name
            self._recover_generation(key, rec)
            self.records[key] = rec
            self.runtime[key] = {"last_change": 0.0, "last_verify": 0.0, "stat": None}
            if rec.get("status") == "active":
                prior = self.by_path.get(rec["path"])
                if prior is None or (self.records[prior].get("first_seen") or "") < (rec.get("first_seen") or ""):
                    self._track(key)
            elif rec.get("status") == "deleted":
                prior = self.deleted_by_path.get(rec["path"])
                if prior is None or (self.records[prior].get("deleted_at") or "") < (rec.get("deleted_at") or ""):
                    self.deleted_by_path[rec["path"]] = key

    def _recover_generation(self, key: str, rec: dict) -> None:
        """After a crash between appending bytes and saving the record, the
        generation file is the truth: extend the chain over any extra bytes."""
        gen = self._current(rec)
        if gen is None:
            return
        data_path = self.files_dir / key / gen["mirror"]
        try:
            size = data_path.stat().st_size
        except OSError:
            return
        if size == gen["size"]:
            return
        chain = v.read_jsonl(self.files_dir / key / f"{gen['id']}.chain")
        last = chain[-1] if chain else None
        if last and isinstance(last.get("end"), int) and gen["size"] < last["end"] <= size:
            gen["size"], gen["head"] = last["end"], last["head"]
        if size > gen["size"]:
            hasher = v.chain_hasher(gen["head"])
            with open(data_path, "rb") as fh:
                fh.seek(gen["size"])
                while True:
                    chunk = fh.read(COPY_CHUNK)
                    if not chunk:
                        break
                    hasher.update(chunk)
            gen["size"], gen["head"] = size, hasher.hexdigest()
            self._chain_entry(key, gen, self.clock())
        self._save_record(key, rec)

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #
    def run_forever(self) -> None:
        self.start()
        while True:
            started = self.clock()
            try:
                self.poll_once()
            except Exception as exc:  # noqa: BLE001 - one bad cycle must not stop capture
                self.log(f"poll error: {exc!r}")
            elapsed = self.clock() - started
            time.sleep(max(0.05, self.poll_seconds - elapsed))

    def poll_once(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        self._check_sources(now)
        if now - self._last_rescan >= self.rescan_seconds:
            self._full_scan(now)
        else:
            self._scan_changed_dirs(now)
        cold = now - self._last_cold >= self.cold_seconds
        if cold:
            self._last_cold = now
        for key in list(self.by_path.values()):
            rt = self.runtime.get(key) or {}
            if cold or now - rt.get("last_change", 0.0) < HOT_SECONDS:
                self._stat_and_process(key, now)
        self._resolve_missing(now)
        if self._index_dirty:
            self._write_index()
        self._heartbeat(now)

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #
    def _check_sources(self, now: float) -> None:
        for src in self.sources:
            prev = self.source_state.get(src.name)
            try:
                root = _open_directory(src.root, readable=True)
                try:
                    os.listdir(root)
                finally:
                    os.close(root)
                state = {"ok": True, "error": ""}
            except FileNotFoundError:
                state = {"ok": True, "error": "missing"}
            except PermissionError as exc:
                state = {"ok": False, "error": f"permission denied: {exc.strerror}"}
            except OSError as exc:
                state = {"ok": False, "error": str(exc)}
            if prev is not None and prev["error"] == "missing" and state["ok"] and state["error"] != "missing":
                for dirpath, _dirs, filenames in _walk_directory(src.root):
                    self._note_dir(dirpath, src)
                    self._scan_listing(src, dirpath, filenames, now, False)
            if prev is not None and prev["ok"] != state["ok"]:
                kind = "access_restored" if state["ok"] else "access_lost"
                self._event(None, kind, {"source": src.name, "root": str(src.root), "error": state["error"]})
            elif prev is None and not state["ok"]:
                self._event(None, "access_lost", {"source": src.name, "root": str(src.root), "error": state["error"]})
            self.source_state[src.name] = state

    def _full_scan(self, now: float, preexisting: bool = False) -> None:
        self._last_rescan = now
        seen_dirs = set()
        for src in self.sources:
            if not self.source_state.get(src.name, {"ok": True})["ok"]:
                # Can't look inside: keep its directories as they were rather
                # than reading lost access as every file being deleted.
                seen_dirs.update(d for d, (_m, s) in self.dir_mtimes.items() if s is src)
                continue
            for dirpath, dirnames, filenames in _walk_directory(src.root):
                seen_dirs.add(dirpath)
                self._note_dir(dirpath, src)
                self._scan_listing(src, dirpath, filenames, now, preexisting)
        for dirpath in list(self.dir_mtimes):
            if dirpath not in seen_dirs:
                self._dir_gone(dirpath, now)

    def _note_dir(self, dirpath: str, src: Source) -> None:
        try:
            fd = _open_directory(dirpath)
            try:
                self.dir_mtimes[dirpath] = (_dir_sig(os.fstat(fd)), src)
            finally:
                os.close(fd)
        except OSError:
            self.dir_mtimes.pop(dirpath, None)

    def _scan_changed_dirs(self, now: float) -> None:
        for dirpath, (mtime_ns, src) in list(self.dir_mtimes.items()):
            try:
                fd = _open_directory(dirpath, readable=True)
            except OSError as exc:
                if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                    self._dir_gone(dirpath, now)
                continue
            try:
                st = os.fstat(fd)
                if _dir_sig(st) == mtime_ns:
                    continue
                self.dir_mtimes[dirpath] = (_dir_sig(st), src)
                files, new_dirs = [], []
                with os.scandir(fd) as entries:
                    for entry in entries:
                        child = os.path.join(dirpath, entry.name)
                        if entry.is_dir(follow_symlinks=False) and child not in self.dir_mtimes:
                            new_dirs.append(child)
                        elif entry.is_file(follow_symlinks=False):
                            files.append(entry.name)
            except OSError:
                continue
            finally:
                os.close(fd)
            for child in new_dirs:
                # A new subtree: discover it this cycle, through safe fds.
                try:
                    for sub, _dirs, subfiles in _walk_directory(child):
                        self._note_dir(sub, src)
                        self._scan_listing(src, sub, subfiles, now, False)
                except OSError:
                    continue
            self._scan_listing(src, dirpath, files, now, False)

    def _dir_gone(self, dirpath: str, now: float) -> None:
        """A tracked directory disappeared: forget it and its subdirectories;
        its files are picked up as missing by the per-file stat."""
        prefix = dirpath.rstrip(os.sep) + os.sep
        for d in list(self.dir_mtimes):
            if d == dirpath or d.startswith(prefix):
                del self.dir_mtimes[d]
        for d, paths in list(self.files_by_dir.items()):
            if d == dirpath or d.startswith(prefix):
                for path in paths:
                    if path in self.by_path:
                        self.pending_missing.setdefault(self.by_path[path], now)

    def _scan_listing(self, src: Source, dirpath: str, filenames, now: float, preexisting: bool) -> None:
        rel_dir = os.path.relpath(dirpath, src.root)
        rel_parts_dir = () if rel_dir == "." else tuple(Path(rel_dir).parts)
        present = set()
        for name in filenames:
            rel_parts = rel_parts_dir + (name,)
            if not src.wants(rel_parts):
                continue
            path = os.path.join(dirpath, name)
            present.add(path)
            if path in self.by_path:
                continue
            try:
                st = _stat_nofollow(path)
            except OSError:
                continue
            if not stat_mod.S_ISREG(st.st_mode):
                continue
            self._new_file(src, path, "/".join(rel_parts), st, now, preexisting)
        # Tracked files in this directory that are no longer listed.
        for path in list(self.files_by_dir.get(dirpath, ())):
            if path not in present and path in self.by_path:
                self.pending_missing.setdefault(self.by_path[path], now)

    def _new_file(self, src: Source, path: str, relpath: str, st, now: float, preexisting: bool) -> None:
        # A file that turns up under a new name with the inode of a file that
        # just vanished was moved (e.g. Codex archiving a rollout).
        moved = self.by_inode.get((st.st_dev, st.st_ino))
        if moved is not None and (moved in self.pending_missing or _exists(self.records[moved]["path"]) is False):
            self._moved(moved, path, relpath, src, now)
            self._stat_and_process(moved, now)
            return

        try:
            fd, st = _open_regular(path)
        except OSError as exc:
            if exc.errno not in (errno.ENOENT, errno.ENOTDIR) and path not in self.unreadable_paths:
                self.unreadable_paths.add(path)
                self._event(None, "unreadable", {"path": path, "error": exc.strerror or str(exc)})
            return
        self.unreadable_paths.discard(path)
        key = self._make_key(Path(path).stem)
        rec = {
            "version": 1,
            "key": key,
            "source": src.name,
            "parser": src.parser,
            "path": path,
            "relpath": relpath,
            "path_history": [{"path": path, "t": v.iso_utc(now)}],
            "dev": st.st_dev,
            "ino": st.st_ino,
            "first_seen": v.iso_utc(now),
            "preexisting": preexisting,
            "status": "active",
            "flags": [],
            "generations": [],
            "last_mtime": st.st_mtime,
        }
        # The same path had a transcript that was deleted: this one replaces it.
        if path in self.deleted_by_path:
            rec["recreated_from"] = self.deleted_by_path[path]
        (self.files_dir / key).mkdir()
        self.records[key] = rec
        self.runtime[key] = {"last_change": now, "last_verify": now, "stat": None}
        self._track(key)
        if rec.get("recreated_from"):
            self._flag(key, "recreated", {"previous_key": rec["recreated_from"]})
        with os.fdopen(fd, "rb") as live:
            self._start_generation(key, "initial" if not rec.get("recreated_from") else "recreated", now, live)
        self.runtime[key]["stat"] = _sig(st)
        rec["last_stat"] = _sig(st)
        self._save_record(key, rec)
        self._index_dirty = True

    def _make_key(self, stem: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in stem)[:80] or "file"
        while True:
            key = f"{safe}-{secrets.token_hex(4)}"
            if key not in self.records and not (self.files_dir / key).exists():
                return key

    def _moved(self, key: str, path: str, relpath: str, src: Source, now: float) -> None:
        rec = self.records[key]
        old = rec["path"]
        self.pending_missing.pop(key, None)
        self._untrack(key)
        rec["path"] = path
        rec["relpath"] = relpath
        rec["source"] = src.name
        rec.setdefault("path_history", []).append({"path": path, "t": v.iso_utc(now)})
        self._track(key)
        self._event(key, "moved", {"from": old, "to": path})
        self._save_record(key, rec)
        self._index_dirty = True

    def _resolve_missing(self, now: float, force: bool = False) -> None:
        """A file missing for a full poll (not re-found under another name)
        was deleted."""
        for key, since in list(self.pending_missing.items()):
            rec = self.records.get(key)
            if rec is None or rec.get("status") != "active":
                self.pending_missing.pop(key, None)
                continue
            exists = _exists(rec["path"])
            if exists is None:
                continue  # can't tell; lost access is reported separately
            if exists:
                self.pending_missing.pop(key, None)
                continue
            if not force and now - since < self.poll_seconds:
                continue
            self.pending_missing.pop(key, None)
            gen = self._current(rec)
            self._untrack(key)
            rec["status"] = "deleted"
            rec["deleted_at"] = v.iso_utc(now)
            self.deleted_by_path[rec["path"]] = key
            self._flag(key, "deleted", {
                "captured_size": gen["size"] if gen else 0,
                "last_mtime": rec.get("last_mtime"),
            })
            if gen is not None:
                gen["closed"] = v.iso_utc(now)
                gen["close_reason"] = "deleted"
            self._save_record(key, rec)
            self._index_dirty = True

    # ------------------------------------------------------------------ #
    # Capture
    # ------------------------------------------------------------------ #
    def _stat_and_process(self, key: str, now: float) -> None:
        rec = self.records[key]
        try:
            st = _stat_nofollow(rec["path"])
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                self.pending_missing.setdefault(key, now)
            else:
                self._mark_unreadable(key, exc)
            return
        if not stat_mod.S_ISREG(st.st_mode):
            self.pending_missing.setdefault(key, now)
            return
        self.pending_missing.pop(key, None)
        rt = self.runtime[key]
        sig = _sig(st)
        due_verify = now - rt.get("last_verify", 0.0) >= self.verify_seconds and rt.get("dirty_since_verify")
        if sig == rt.get("stat") and not due_verify:
            return
        self._process(key, rec["path"], st, now, full_verify=bool(due_verify))

    def _process(self, key: str, path: str, st, now: float, full_verify: bool = False, offline: bool = False) -> None:
        rec = self.records[key]
        rt = self.runtime[key]
        detail_base = {"while_offline": True} if offline else {}
        try:
            fd, fst = _open_regular(path)
        except OSError as exc:
            if exc.errno in (errno.ENOENT, errno.ENOTDIR):
                self.pending_missing.setdefault(key, now)
            else:
                self._mark_unreadable(key, exc)
            return
        try:
            if rt.get("unreadable"):
                rt["unreadable"] = False
            rt["stat"] = _sig(fst)
            rec["last_stat"] = _sig(fst)
            rec["last_mtime"] = fst.st_mtime
            with os.fdopen(fd, "rb", closefd=False) as live:
                self._process_open(key, rec, rt, live, fst, now, full_verify, detail_base)
        finally:
            os.close(fd)

    def _mark_unreadable(self, key: str, exc: OSError) -> None:
        rt = self.runtime[key]
        if not rt.get("unreadable"):
            rt["unreadable"] = True
            self._event(key, "unreadable", {"error": exc.strerror or str(exc)})

    def _process_open(self, key, rec, rt, live, st, now, full_verify, detail_base) -> None:
        gen = self._current(rec)
        identity_changed = (st.st_dev, st.st_ino) != (rec.get("dev"), rec.get("ino"))
        if identity_changed:
            self._untrack(key)
            rec["dev"], rec["ino"] = st.st_dev, st.st_ino
            self._track(key)
            if gen is not None and self._prefix_matches(key, gen, live, st.st_size, full=True):
                self._event(key, "inode_changed", dict(detail_base))
                rt["last_verify"] = now
                rt["dirty_since_verify"] = False
                self._append(key, gen, live, now)
                return
            if gen is not None:
                self._diverge(key, "replaced", live, st, now, detail_base)
                return
        if gen is None:
            # Emptied earlier; start again once there is content.
            if st.st_size > 0:
                self._start_generation(key, rec.get("awaiting_reason") or "initial", now, live)
            return
        if st.st_size < gen["size"]:
            self._diverge(key, "truncated", live, st, now, detail_base)
            return
        # Transcripts only grow. A change that doesn't add bytes (same size,
        # new ctime) is an edit or a metadata touch: compare everything now
        # rather than just the tail.
        if st.st_size == gen["size"]:
            full_verify = True
        if not self._prefix_matches(key, gen, live, st.st_size, full=full_verify):
            self._diverge(key, "rewritten", live, st, now, detail_base)
            return
        if full_verify:
            rt["last_verify"] = now
            rt["dirty_since_verify"] = False
        self._append(key, gen, live, now)

    def _prefix_matches(self, key: str, gen: dict, live, live_size: int, full: bool) -> bool:
        """Do the live file's first gen['size'] bytes equal the capture?
        Checks only the tail window unless ``full``."""
        size = gen["size"]
        if live_size < size:
            return False
        start = 0 if full else max(0, size - TAIL_WINDOW)
        data_path = self.files_dir / key / gen["mirror"]
        with open(data_path, "rb") as cap:
            cap.seek(start)
            live.seek(start)
            remaining = size - start
            while remaining > 0:
                n = min(COPY_CHUNK, remaining)
                if cap.read(n) != live.read(n):
                    return False
                remaining -= n
        return True

    def _append(self, key: str, gen: dict, live, now: float) -> None:
        live.seek(gen["size"])
        data_path = self.files_dir / key / gen["mirror"]
        added = 0
        hasher = v.chain_hasher(gen["head"])
        with open(data_path, "ab") as out:
            while True:
                chunk = live.read(COPY_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                hasher.update(chunk)
                added += len(chunk)
        if not added:
            return
        gen["size"] += added
        gen["head"] = hasher.hexdigest()
        self._chain_entry(key, gen, now)
        rt = self.runtime[key]
        rt["last_change"] = now
        rt["dirty_since_verify"] = True
        self._save_record(key, self.records[key])

    def _start_generation(self, key: str, reason: str, now: float, live=None) -> None:
        rec = self.records[key]
        n = len(rec["generations"])
        gid = v.gen_id(n)
        mirror = f"{gid}/{rec['source']}/{rec['relpath']}"
        data_path = self.files_dir / key / mirror
        data_path.parent.mkdir(parents=True, exist_ok=True)
        data_path.touch()
        gen = {
            "id": gid,
            "mirror": mirror,
            "reason": reason,
            "started": v.iso_utc(now),
            "closed": None,
            "close_reason": None,
            "size": 0,
            "head": v.chain_seed(key, gid),
        }
        rec["generations"].append(gen)
        rec.pop("awaiting_reason", None)
        self.runtime[key]["last_verify"] = now
        self.runtime[key]["dirty_since_verify"] = False
        if live is None:
            fd, _st = _open_regular(rec["path"])
            with os.fdopen(fd, "rb") as fh:
                self._append(key, gen, fh, now)
        else:
            self._append(key, gen, live, now)
        self._save_record(key, rec)
        self._index_dirty = True

    def _diverge(self, key: str, reason: str, live, st, now: float, detail_base: dict) -> None:
        """The live file is no longer an extension of the capture: freeze the
        current generation, record why, and start capturing afresh."""
        rec = self.records[key]
        gen = self._current(rec)
        detail = dict(detail_base)
        if gen is not None:
            detail.update({"generation": gen["id"], "captured_size": gen["size"], "live_size": st.st_size})
            gen["closed"] = v.iso_utc(now)
            gen["close_reason"] = reason
        self._flag(key, reason, detail)
        if st.st_size > 0:
            self._start_generation(key, reason, now, live)
        else:
            rec["awaiting_reason"] = reason
            self._save_record(key, rec)
            self._index_dirty = True
        self.runtime[key]["last_change"] = now

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    @staticmethod
    def _current(rec: dict):
        gens = rec.get("generations") or []
        if gens and not gens[-1].get("closed"):
            return gens[-1]
        return None

    def _chain_entry(self, key: str, gen: dict, now: float) -> None:
        line = json.dumps({"end": gen["size"], "head": gen["head"], "t": v.iso_utc(now)})
        with open(self.files_dir / key / f"{gen['id']}.chain", "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def _save_record(self, key: str, rec: dict) -> None:
        v.atomic_write_json(self.files_dir / key / v.RECORD_FILE, rec)

    def _flag(self, key: str, kind: str, detail: dict) -> None:
        rec = self.records[key]
        if kind not in rec["flags"]:
            rec["flags"].append(kind)
        self._event(key, kind, detail)
        self._index_dirty = True

    def _event(self, key, kind: str, detail: dict) -> None:
        now = self.clock()
        event = {"t": v.iso_utc(now), "type": kind}
        if key is not None:
            event["key"] = key
            event["path"] = self.records[key].get("path")
        if detail:
            event["detail"] = detail
        line = json.dumps(event, ensure_ascii=False) + "\n"
        with open(self.root / v.EVENTS_FILE, "a", encoding="utf-8") as fh:
            fh.write(line)
        if key is not None:
            with open(self.files_dir / key / v.EVENTS_FILE, "a", encoding="utf-8") as fh:
                fh.write(line)
        self.log(line.rstrip())

    def _write_index(self) -> None:
        files = {}
        for key, rec in self.records.items():
            gens = rec.get("generations") or []
            files[key] = {
                "source": rec.get("source"),
                "parser": rec.get("parser"),
                "path": rec.get("path"),
                "status": rec.get("status"),
                "flags": list(rec.get("flags") or []),
                "gens": len(gens),
                "deleted_at": rec.get("deleted_at"),
                "last_mtime": rec.get("last_mtime"),
                "recreated_from": rec.get("recreated_from"),
            }
        v.atomic_write_json(self.root / v.INDEX_FILE, {"version": 1, "files": files})
        self._index_dirty = False

    def _heartbeat(self, now: float, force: bool = False) -> None:
        if not force and now - self._last_heartbeat < HEARTBEAT_SECONDS:
            return
        self._last_heartbeat = now
        v.atomic_write_json(self.root / v.STATUS_FILE, {
            "version": 1,
            "pid": os.getpid(),
            "started": v.iso_utc(self.started_at or now),
            "last_poll": v.iso_utc(now),
            "poll_seconds": self.poll_seconds,
            "n_files": len(self.records),
            "sources": {
                src.name: dict(root=str(src.root), parser=src.parser, **self.source_state.get(src.name, {}))
                for src in self.sources
            },
        })


# ---------------------------------------------------------------------------
# Integrity check of the store itself
# ---------------------------------------------------------------------------
def verify_store(root) -> list:
    """Recompute every generation's hash chain; return a list of problems."""
    root = Path(root)
    problems = []
    files_dir = root / v.FILES_DIR
    if not files_dir.is_dir():
        return [f"{files_dir} does not exist"]
    for entry in sorted(os.scandir(files_dir), key=lambda e: e.name):
        if not entry.is_dir(follow_symlinks=False):
            continue
        key = entry.name
        rec = v.read_json(Path(entry.path) / v.RECORD_FILE)
        if not isinstance(rec, dict):
            problems.append(f"{key}: unreadable record.json")
            continue
        for gen in rec.get("generations") or []:
            gid = gen.get("id")
            data_path = Path(entry.path) / gen.get("mirror", "")
            chain = v.read_jsonl(Path(entry.path) / f"{gid}.chain")
            head = v.chain_seed(key, gid)
            offset = 0
            try:
                with open(data_path, "rb") as fh:
                    for link in chain:
                        end = link.get("end")
                        if not isinstance(end, int) or end < offset:
                            problems.append(f"{key}/{gid}: malformed chain entry {link}")
                            break
                        hasher = v.chain_hasher(head)
                        remaining = end - offset
                        while remaining > 0:
                            chunk = fh.read(min(COPY_CHUNK, remaining))
                            if not chunk:
                                break
                            hasher.update(chunk)
                            remaining -= len(chunk)
                        if remaining:
                            problems.append(f"{key}/{gid}: data shorter than chain ({end} bytes expected)")
                            break
                        head = hasher.hexdigest()
                        if head != link.get("head"):
                            problems.append(f"{key}/{gid}: hash mismatch at byte {end}")
                            break
                        offset = end
                    else:
                        size = data_path.stat().st_size
                        if size != offset:
                            problems.append(f"{key}/{gid}: {size - offset} bytes not covered by the chain")
                        if head != gen.get("head"):
                            problems.append(f"{key}/{gid}: record head does not match chain")
            except OSError as exc:
                problems.append(f"{key}/{gid}: {exc}")
    return problems
