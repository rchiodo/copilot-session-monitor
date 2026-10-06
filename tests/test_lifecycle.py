"""Tests for pymonitor.lifecycle -- the literal port of lifecycle.mjs.

No dedicated lifecycle.test.mjs existed in the Node app, so this suite is
newly authored (not ported) but exercises the exact same surface described by
lifecycle.mjs: single-role-per-directory lock acquisition, stale-lock
replacement when the previous owner's PID is dead, conflict when the
previous owner is still alive, and identity-safe release (a release call
only removes the lock file if it still names the identity that call wrote).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pymonitor import lifecycle as lc


def _lock_path(directory: Path, role: str) -> Path:
    return directory / f"{role}.lock"


async def test_acquire_role_creates_lock_file_with_pid_and_identity(tmp_path: Path) -> None:
    await lc.acquire_role(tmp_path, "collector")
    lock = _lock_path(tmp_path, "collector")
    assert lock.exists()
    content = json.loads(lock.read_text(encoding="utf-8"))
    assert content["pid"] == os.getpid()
    assert isinstance(content["identity"], str) and len(content["identity"]) > 0


async def test_acquire_role_rejects_when_lock_names_a_live_pid(tmp_path: Path) -> None:
    await lc.acquire_role(tmp_path, "collector")
    with pytest.raises(RuntimeError, match="collector already owns this directory"):
        await lc.acquire_role(tmp_path, "collector")


async def test_acquire_role_replaces_a_stale_lock_naming_a_dead_pid(tmp_path: Path) -> None:
    lock = _lock_path(tmp_path, "collector")
    lock.write_text(json.dumps({"pid": 999_999_999, "identity": "stale"}), encoding="utf-8")

    await lc.acquire_role(tmp_path, "collector")

    content = json.loads(lock.read_text(encoding="utf-8"))
    assert content["pid"] == os.getpid()
    assert content["identity"] != "stale"


async def test_acquire_role_different_roles_do_not_conflict(tmp_path: Path) -> None:
    await lc.acquire_role(tmp_path, "collector")
    await lc.acquire_role(tmp_path, "watcher")
    assert _lock_path(tmp_path, "collector").exists()
    assert _lock_path(tmp_path, "watcher").exists()


async def test_release_removes_the_lock_file(tmp_path: Path) -> None:
    release = await lc.acquire_role(tmp_path, "collector")
    lock = _lock_path(tmp_path, "collector")
    assert lock.exists()
    await release()
    assert not lock.exists()


async def test_release_is_a_noop_when_lock_file_already_gone(tmp_path: Path) -> None:
    release = await lc.acquire_role(tmp_path, "collector")
    _lock_path(tmp_path, "collector").unlink()
    await release()  # must not raise


async def test_release_does_not_remove_a_lock_written_by_a_newer_acquisition(tmp_path: Path) -> None:
    lock = _lock_path(tmp_path, "collector")
    release1 = await lc.acquire_role(tmp_path, "collector")

    # Simulate the first owner's process dying and a second process
    # replacing the (now-stale) lock before the first owner's release()
    # callable is ever invoked.
    lock.write_text(json.dumps({"pid": 999_999_999, "identity": "stale"}), encoding="utf-8")
    await lc.acquire_role(tmp_path, "collector")
    second_owner_content = json.loads(lock.read_text(encoding="utf-8"))

    await release1()

    assert lock.exists()
    assert json.loads(lock.read_text(encoding="utf-8")) == second_owner_content


async def test_acquire_role_after_release_succeeds(tmp_path: Path) -> None:
    release = await lc.acquire_role(tmp_path, "collector")
    await release()
    # Should not raise: the lock is gone, so this is a fresh acquisition.
    release2 = await lc.acquire_role(tmp_path, "collector")
    await release2()
