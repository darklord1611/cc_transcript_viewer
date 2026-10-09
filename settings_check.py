"""Read-only checks for the viewer setup settings documented in README.md.

These recommendations affect what agents retain, never Mica's trust checks.
Only the relevant settings are returned; other config contents stay private.
"""

from __future__ import annotations

import json
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python 3.9/3.10: skip the Codex check rather than add a dependency.
    tomllib = None

CLAUDE_MIN_RETENTION_DAYS = 10000


def _read(path: Path, parser) -> tuple:
    """(settings dict, error message); a missing file is just empty."""
    try:
        data = parser(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, ""
    except (OSError, ValueError, UnicodeError):
        return {}, "Cannot read or parse this settings file."
    if not isinstance(data, dict):
        return {}, "Cannot read or parse this settings file."
    return data, ""


def check_settings(claude_home: Path, codex_home: Path) -> dict:
    issues = []

    def issue(agent, path, setting, value, reason):
        issues.append({"id": f"{agent}:{setting}", "agent": agent, "path": str(path),
                       "setting": setting, "recommended": value, "reason": reason})

    # Only check agents whose home folders exist (the server passes any CLI
    # directory overrides).
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

    if tomllib is not None and codex_home.exists():
        path = codex_home / "config.toml"
        data, error = _read(path, tomllib.loads)
        setting = "model_reasoning_summary"
        summary = data.get(setting)
        # An active profile can override the top-level value.
        profile = data.get("profile")
        profiles = data.get("profiles")
        active = profiles.get(profile) if isinstance(profiles, dict) and isinstance(profile, str) else None
        if isinstance(active, dict) and setting in active:
            summary = active[setting]
            setting = f"profiles.{profile}.{setting}"
        if error or summary != "detailed":
            issue("Codex", path, setting, "detailed",
                  error or "Save detailed reasoning summaries in new sessions.")
    return {"issues": issues}
