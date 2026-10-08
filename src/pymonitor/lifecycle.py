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

Second deviation, added after two collector hosts were observed running
simultaneously against the same data directory: the PID-file lock below is
check-then-act (read the lock, probe liveness, maybe delete, then
atomically create) and the "probe + maybe delete" steps are not atomic with
the final create. Two processes racing through that window can both reach
the final `O_CREAT | O_EXCL` create; before this fix the loser's
`FileExistsError` was uncaught, so it "crashed" rather than failing with the
expected "already owns this directory" error -- but a crash late enough that
a prior start already got the collector's HTTP server listening would still
look, from outside, like one dead + one live process rather than a clean
rejection. On Windows, a named mutex (auto-released by the OS even if the
owning process is killed or crashes, no PID-staleness guessing required) is
now acquired first as the authoritative single-instance gate; the PID-file
lock remains underneath (all platforms) for diagnostics (readable
pid/identity on disk) and as the sole gate on non-Windows platforms.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Awaitable, Callable

import psutil

try:  # pragma: no cover - import guard for non-Windows dev/test environments
    import win32event
except ImportError:  # pragma: no cover
    win32event = None  # type: ignore[assignment]

__all__ = ["acquire_role"]


def _mutex_name(directory: Path, role: str) -> str:
    """Derive a Windows named-mutex name for ``(directory, role)``.

    Named kernel objects have practical length/character limits, so the
    (potentially long) absolute directory path is hashed rather than used
    verbatim. The `Local\\` prefix scopes the mutex to the current login
    session, matching the loopback-only, per-user nature of this app.
    """
    digest = hashlib.sha256(f"{directory.resolve()}::{role}".encode("utf-8")).hexdigest()
    return f"Local\\pymonitor-{role}-{digest[:32]}"


def _acquire_mutex(directory: Path, role: str) -> Callable[[], None] | None:
    """Non-blocking acquire of the Windows single-instance mutex.

    Returns a zero-argument release callable on success, or ``None`` on
    platforms without `pywin32` (non-Windows), where the PID-file lock is
    the sole gate. Raises `RuntimeError` if another live process already
    holds the mutex.
    """
    if win32event is None:
        return None

    handle = win32event.CreateMutex(None, False, _mutex_name(directory, role))
    result = win32event.WaitForSingleObject(handle, 0)
    if result == win32event.WAIT_TIMEOUT:
        raise RuntimeError(
            f"{role} already owns this directory (or its old PID was reused); "
            "verify the existing process first"
        )
    # WAIT_OBJECT_0: acquired cleanly. WAIT_ABANDONED: the previous owner
    # exited/crashed/was killed without releasing -- Windows still grants us
    # ownership, which is exactly the "stale lock" case the PID-file branch
    # below handles manually.

    def release() -> None:
        # Best-effort and idempotent, like the file-lock release below: a
        # second release of an already-released mutex (e.g. a delayed
        # duplicate cleanup call racing a newer owner) must not raise.
        try:
            win32event.ReleaseMutex(handle)
        except Exception:
            pass
        handle.Close()

    return release


async def acquire_role(directory: Path, role: str) -> Callable[[], Awaitable[None]]:
    """Claim exclusive ownership of ``role`` within ``directory``.

    On Windows, first acquires a named mutex (see `_acquire_mutex`) as the
    authoritative, race-free gate -- the OS releases it automatically even
    if the owning process is killed, so no PID-staleness guessing is
    needed. Then writes a ``<role>.lock`` file containing the current PID
    and a random identity, for diagnostics (and as the sole gate on
    non-Windows platforms). If a lock file already exists and names a
    still-alive PID, raises. If it exists but names a dead PID (stale lock
    left behind by a crash), it is removed and replaced. Returns an async
    "release" callable that removes the lock file -- but only if it still
    names the identity this call wrote, so a release from a stale/
    superseded acquisition can never delete a lock a newer process
    legitimately holds -- and releases the mutex, if one was acquired.
    """
    release_mutex = _acquire_mutex(directory, role)

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

    try:
        fd = os.open(file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise RuntimeError(
            f"{role} already owns this directory (or its old PID was reused); "
            "verify the existing process first"
        ) from None
    try:
        os.write(fd, json.dumps({"pid": os.getpid(), "identity": identity}).encode("utf-8"))
    finally:
        os.close(fd)

    async def release() -> None:
        try:
            current = json.loads(file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        else:
            if current.get("identity") == identity:
                file.unlink()
        if release_mutex is not None:
            release_mutex()

    return release
