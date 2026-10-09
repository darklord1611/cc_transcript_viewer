"""Tamper-resistant backup of agent transcripts ("Mica").

Coding agents run as your Unix user, so they can edit or delete the transcript
files this viewer reads. Mica is an optional companion daemon that
runs as a *separate* system user, copies every transcript append into a folder
only that user can write, and records an event whenever a live transcript is
truncated, rewritten, replaced, or deleted. The viewer reads the store
(read-only) to flag transcripts that no longer match what was captured.

Modules:
    store    on-disk format, shared helpers, and the read-only StoreReader
    capture  the polling capture engine the daemon runs
    install  macOS installer (system user, LaunchDaemon, ACLs)
    __main__ command line: daemon / status / verify / install / uninstall

Everything here is standard library only and runs on Python 3.9, because the
installed daemon uses Apple's root-owned /usr/bin/python3 rather than any
interpreter an agent could modify.
"""
