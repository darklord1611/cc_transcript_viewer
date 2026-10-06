"""Read-only checks for the viewer setup settings documented in README.md.

These recommendations affect what agents retain, never Mica's trust checks.
Only the relevant settings are returned; other config contents stay private.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python 3.9/3.10: keep the viewer dependency-free.
    tomllib = None

CLAUDE_MIN_RETENTION_DAYS = 10000
_CACHE = {}
_LOCK = threading.Lock()


def _codex_config(text: str) -> dict:
    if tomllib is not None:
        return tomllib.loads(text)
    # On older Python, extract only the string settings we need. A table's
    # values must not be mistaken for global settings. Support active profiles
    # too, so an override to "none" does not look like detailed summaries.
    data = {}
    target = data
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            profile = re.fullmatch(r'\[profiles\.([A-Za-z0-9_-]+|"[^"\\]+"|\x27[^\x27]+\x27)\]\s*(?:#.*)?', stripped)
            if profile:
                name = profile[1].strip("\"'")
                target = data.setdefault("profiles", {}).setdefault(name, {})
            else:
                target = None
            continue
        if target is None:
            continue
        match = re.fullmatch(
            r'(?:"|\x27)?(model_reasoning_summary|profile)(?:"|\x27)?\s*=\s*'
            r'("(?:\\.|[^"\\])*"|\x27[^\x27]*\x27)\s*(?:#.*)?', stripped,
        )
        if match:
            key, raw = match.groups()
            if key in target:
                raise ValueError(f"duplicate {key}")
            target[key] = json.loads(raw) if raw.startswith('"') else raw[1:-1]
    return data


def _read(path: Path, parser) -> tuple:
    try:
        st = path.stat()
        fp = (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino)
    except FileNotFoundError:
        return {}, ""
    except OSError:
        return {}, "Cannot read this settings file."
    with _LOCK:
        cached = _CACHE.get(path)
        if cached is not None and cached[0] == fp:
            return cached[1]
        try:
            data = parser(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("settings must be an object")
            result = data, ""
        except (OSError, ValueError, UnicodeError):
            result = {}, "Cannot read or parse this settings file."
        _CACHE[path] = fp, result
        return result


def check_settings(claude_home: Path, codex_home: Path) -> dict:
    issues = []

    def issue(agent, path, setting, value, reason):
        issues.append({"id": f"{agent}:{setting}", "agent": agent, "path": str(path),
                       "setting": setting, "recommended": value, "reason": reason})

    # Avoid recommending settings for agents whose configured homes do not
    # exist. CLI directory overrides are passed in by the server.
    if claude_home.exists():
        path = claude_home / "settings.json"
        data, error = _read(path, json.loads)
        days = data.get("cleanupPeriodDays")
        if error or not isinstance(days, int) or isinstance(days, bool) or days < CLAUDE_MIN_RETENTION_DAYS:
            issue("Claude Code", path, "cleanupPeriodDays", CLAUDE_MIN_RETENTION_DAYS,
                  error or "Keep original transcripts from being automatically deleted.")
        if error or data.get("showThinkingSummaries") is not True:
            issue("Claude Code", path, "showThinkingSummaries", True,
                  error or "Save readable thinking summaries in new sessions.")

    if codex_home.exists():
        path = codex_home / "config.toml"
        data, error = _read(path, _codex_config)
        summary = data.get("model_reasoning_summary")
        profiles = data.get("profiles")
        profile = data.get("profile")
        active = profiles.get(profile) if isinstance(profiles, dict) and isinstance(profile, str) else None
        if isinstance(active, dict):
            summary = active.get("model_reasoning_summary", summary)
        if error or summary != "detailed":
            setting = f"profiles.{profile}.model_reasoning_summary" if isinstance(active, dict) and "model_reasoning_summary" in active else "model_reasoning_summary"
            issue("Codex", path, setting, "detailed",
                  error or "Save detailed reasoning summaries in new sessions.")
    return {"issues": issues}
