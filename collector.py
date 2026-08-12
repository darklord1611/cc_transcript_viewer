#!/usr/bin/env python3
"""Pull transcripts from remote pods into a local mirror the viewer serves.

This is the *only* networked component of the setup (Phase 2 of the multi-pod
auditing-game oversight). It reads a ``pods.json`` registry, and for each pod
copies its transcript directories over SSH into

    <mirror>/<pod-id>/{claude/projects, codex/sessions, codex/archived_sessions}

then writes ``<mirror>/<pod-id>/pod.json`` with the pod's team/game labels. The
viewer, run separately with ``server.py --mirror <mirror>``, reads that tree and
never touches the network itself — so the server's "no outbound connections"
guarantee stays intact. All reach-out lives here, on purpose.

Transport: ``ssh 'tar -c' | tar -x`` into a staging dir, then a *local*
``rsync -a --delete`` into the live mirror. tar needs nothing installed on the
pod (RunPod images often lack rsync) and preserves mtimes, so the viewer's
mtime-keyed summary cache keeps working; the local rsync makes each file appear
atomically and propagates deletions. Pods being ephemeral, the mirror is durable
and outlives them — which is what an audit record wants.

Usage:
    python3 collector.py --config pods.json --mirror ./mirror --once
    python3 collector.py --config pods.json --mirror ./mirror --interval 20

pods.json:
    {
      "pods": [
        {
          "id": "red-domain-code", "team": "red", "game": "domain code",
          "host": "<POD_IP>", "port": 22, "user": "root",
          "key": "~/.ssh/id_ed25519"
        }
      ]
    }

Optional per-pod overrides: "label", "claude_path" (default ~/.claude/projects),
"codex_home" (default ~/.codex).
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

DEFAULT_CLAUDE_PATH = "~/.claude/projects"
DEFAULT_CODEX_HOME = "~/.codex"
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=accept-new",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=15",
    "-o", "ServerAliveInterval=10",
]


def _log(msg: str) -> None:
    print(f"[collector] {msg}", flush=True)


def _ssh_base(pod: dict) -> list[str]:
    cmd = ["ssh", *SSH_OPTS, "-p", str(pod.get("port", 22))]
    key = pod.get("key")
    if key:
        cmd += ["-i", str(Path(key).expanduser())]
    cmd.append(f"{pod.get('user', 'root')}@{pod['host']}")
    return cmd


def _remote_split(remote_path: str) -> tuple[str, str]:
    """(parent, name) of a remote path; ~ is left intact for remote-shell expansion."""
    p = remote_path.rstrip("/")
    parent, _, name = p.rpartition("/")
    return (parent or "/", name or p)


def _pull_tree(pod: dict, remote_parent: str, remote_names: list[str], dest: Path) -> bool:
    """tar a remote subtree over SSH and extract it into ``dest`` (fresh).

    ``dest`` is wiped first so files deleted on the pod don't linger and defeat
    the later --delete. Returns False (quietly) when the remote dir is absent.
    """
    if not remote_names:
        return False
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    quoted = " ".join(f"'{n}'" for n in remote_names)
    # cd (with ~ expansion) so a missing dir fails cleanly instead of tarring junk.
    remote_cmd = f"cd {remote_parent} 2>/dev/null && tar -c --format=posix {quoted}"
    ssh = subprocess.Popen(
        _ssh_base(pod) + [remote_cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    tar = subprocess.Popen(
        ["tar", "-x", "-p", "-C", str(dest)], stdin=ssh.stdout, stderr=subprocess.PIPE
    )
    ssh.stdout.close()  # let ssh receive SIGPIPE if tar dies
    _, tar_err = tar.communicate()
    ssh_err = ssh.stderr.read()
    ssh.wait()
    if ssh.returncode != 0 or tar.returncode != 0:
        detail = (ssh_err or tar_err or b"").decode(errors="replace").strip()
        if detail:
            _log(f"  {pod['id']}: skip {remote_parent}/{quoted} ({detail.splitlines()[-1]})")
        return False
    return True


def _remote_existing(pod: dict, candidates: list[str]) -> list[str]:
    """Subset of remote dir paths that exist, in one SSH round-trip."""
    checks = "; ".join(f'[ -d {c} ] && echo {c}' for c in candidates)
    try:
        out = subprocess.run(
            _ssh_base(pod) + [checks], capture_output=True, timeout=30
        ).stdout.decode(errors="replace")
    except (subprocess.SubprocessError, OSError):
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _publish(staging: Path, live: Path) -> None:
    """Atomically (per file) sync staging into the live mirror; drop removed files."""
    live.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["rsync", "-a", "--delete", f"{staging}/", f"{live}/"],
        check=True,
        capture_output=True,
    )


def sync_pod(pod: dict, mirror: Path) -> str:
    pod_id = pod["id"]
    staging = mirror / ".staging" / pod_id
    live = mirror / pod_id
    got = []

    # Claude Code: <claude_path> -> <pod>/claude/projects. Probe first so a pod
    # whose agent hasn't written yet reports "no transcripts" quietly instead of
    # a tar failure.
    claude_path = pod.get("claude_path", DEFAULT_CLAUDE_PATH)
    if _remote_existing(pod, [claude_path]):
        c_parent, c_name = _remote_split(claude_path)
        if _pull_tree(pod, c_parent, [c_name], staging / "claude"):
            _publish(staging / "claude", live / "claude")
            got.append("claude")

    # Codex: <codex_home>/{sessions,archived_sessions} -> <pod>/codex/...
    codex_home = pod.get("codex_home", DEFAULT_CODEX_HOME)
    codex_dirs = _remote_existing(
        pod, [f"{codex_home}/sessions", f"{codex_home}/archived_sessions"]
    )
    codex_names = [d.rsplit("/", 1)[1] for d in codex_dirs]
    if _pull_tree(pod, codex_home, codex_names, staging / "codex"):
        _publish(staging / "codex", live / "codex")
        got.append("codex")

    # pod.json is what the viewer reads for the team/game/pod tags.
    live.mkdir(parents=True, exist_ok=True)
    meta = {"pod": pod_id, "team": pod.get("team", ""), "game": pod.get("game", "")}
    if pod.get("label"):
        meta["label"] = pod["label"]
    (live / "pod.json").write_text(json.dumps(meta, indent=2))

    return f"{pod_id}: {', '.join(got) if got else 'no transcripts'}"


def load_registry(config: Path) -> list[dict]:
    data = json.loads(config.read_text(encoding="utf-8"))
    pods = data.get("pods") if isinstance(data, dict) else data
    if not isinstance(pods, list):
        raise ValueError("pods.json must be a list of pods or {\"pods\": [...]}")
    for p in pods:
        for required in ("id", "host"):
            if not p.get(required):
                raise ValueError(f"pod missing required field {required!r}: {p}")
    return pods


def sync_once(pods: list[dict], mirror: Path, workers: int) -> None:
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(pods) or 1))) as pool:
        for line in pool.map(lambda p: sync_pod(p, mirror), pods):
            _log(line)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=Path("pods.json"))
    ap.add_argument("--mirror", type=Path, default=Path("mirror"))
    ap.add_argument("--interval", type=float, default=20.0, help="seconds between sync cycles")
    ap.add_argument("--once", action="store_true", help="sync once and exit")
    ap.add_argument("--workers", type=int, default=6, help="max pods synced in parallel")
    args = ap.parse_args()

    pods = load_registry(args.config.expanduser())
    args.mirror.mkdir(parents=True, exist_ok=True)
    _log(f"{len(pods)} pod(s) -> {args.mirror.resolve()}")

    if args.once:
        sync_once(pods, args.mirror, args.workers)
        return
    try:
        while True:
            start = time.monotonic()
            # Re-read the registry each cycle so pods added to pods.json are
            # picked up live, with no collector restart. Keep the last good list
            # if the file is mid-edit or malformed.
            try:
                new_pods = load_registry(args.config.expanduser())
                if len(new_pods) != len(pods):
                    _log(f"registry now has {len(new_pods)} pod(s)")
                pods = new_pods
            except (OSError, ValueError) as e:
                _log(f"registry reload failed, keeping previous ({e})")
            sync_once(pods, args.mirror, args.workers)
            time.sleep(max(0.0, args.interval - (time.monotonic() - start)))
    except KeyboardInterrupt:
        _log("stopped")


if __name__ == "__main__":
    main()
