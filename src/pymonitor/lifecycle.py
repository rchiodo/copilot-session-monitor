"""Port of src/lifecycle.mjs.

Literal port of the single-role-per-directory lock used to ensure only one
collector (or one watcher) process owns a given data directory at a time.

One deviation from the Node original: Node's liveness check uses
`process.kill(pid, 0)` (a no-op signal that only probes existence, raising
`ESRCH` if the PID is gone). Python's `os.kill` has no safe equivalent on
Windows -- passing signal `0` there is special-cased by CPython to call
`TerminateProcess`, which would kill the previous owner outright instead of
merely probing it. `psutil.pid_exists(pid)` is used instead: it performs the
same "is this PID alive" probe (via `OpenProcess`/`kill(pid, 0)` depending on
platform) without that Windows footgun, and `psutil` is already a dependency
of this package for process-table lookups elsewhere.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Awaitable, Callable

import psutil

__all__ = ["acquire_role"]


async def acquire_role(directory: Path, role: str) -> Callable[[], Awaitable[None]]:
    """Claim exclusive ownership of ``role`` within ``directory``.

    Writes a ``<role>.lock`` file containing the current PID and a random
    identity. If a lock file already exists and names a still-alive PID,
    raises. If it exists but names a dead PID (stale lock left behind by a
    crash), it is removed and replaced. Returns an async "release" callable
    that removes the lock file -- but only if it still names the identity
    this call wrote, so a release from a stale/superseded acquisition can
    never delete a lock a newer process legitimately holds.
    """
    file = directory / f"{role}.lock"
    identity = str(uuid.uuid4())
    try:
        previous = json.loads(file.read_text(encoding="utf-8"))
        alive = psutil.pid_exists(previous.get("pid"))
        if alive:
            raise RuntimeError(
                f"{role} already owns this directory (or its old PID was reused); "
                "verify the existing process first"
            )
        file.unlink()
    except FileNotFoundError:
        pass

    fd = os.open(file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(fd, json.dumps({"pid": os.getpid(), "identity": identity}).encode("utf-8"))
    finally:
        os.close(fd)

    async def release() -> None:
        try:
            current = json.loads(file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        if current.get("identity") == identity:
            file.unlink()

    return release
