"""Port of test/reconnect-lease.test.mjs -- regression test for the
"Unconfirmed" bug: forcing a gap on every dropped reporting lease (rather
than only on a genuine wall-clock gap) used to flip still-working sessions
to 'unknown' on a brief reconnect. Preserves that fix byte-for-byte.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pymonitor.families import FamilyMonitor
from pymonitor.local_report import poll_local
from pymonitor.source import LocalSource

SESSION_ID = "55555555-5555-5555-5555-555555555555"


async def _busy_copilot_home(home: Path) -> LocalSource:
    (home / "session-state").mkdir(parents=True)
    db_file = home / "data.db"
    conn = sqlite3.connect(str(db_file))
    conn.executescript(
        """
        CREATE TABLE sessions (id TEXT, title TEXT, is_running INTEGER, was_interrupted INTEGER,
          execution_location TEXT, session_type TEXT, archived_at TEXT);
        CREATE TABLE workspaces (session_id TEXT, host_id TEXT, id TEXT, creator_session_id TEXT, archived_at TEXT);
        CREATE TABLE workspace_parent_links (child_workspace_id TEXT, parent_workspace_id TEXT);
        CREATE TABLE workspace_session_aliases (session_id TEXT, workspace_id TEXT);
        CREATE TABLE workspace_side_chats (workspace_id TEXT, session_id TEXT);
        CREATE TABLE session_side_chats (parent_session_id TEXT, session_id TEXT);
        """
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,1,0,?,?,NULL)", (SESSION_ID, "Busy work", "local", "project")
    )
    conn.execute(
        "INSERT INTO workspaces(session_id,host_id,id) VALUES (?,?,?)", (SESSION_ID, "local", "local-workspace")
    )
    conn.commit()
    conn.close()

    folder = home / "session-state" / SESSION_ID
    folder.mkdir()
    (folder / f"inuse.{os.getpid()}.lock").write_text(str(os.getpid()), encoding="utf-8")
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    events = "\n".join(
        json.dumps(entry)
        for entry in (
            {
                "id": "first",
                "type": "session.start",
                "timestamp": now_iso,
                "data": {"context": {"cwd": "C:\\demo\\project"}},
            },
            {
                "id": "start",
                "type": "assistant.turn_start",
                "timestamp": now_iso,
                "data": {"turnId": "0", "interactionId": "run"},
            },
        )
    ) + "\n"
    (folder / "events.jsonl").write_text(events, encoding="utf-8")

    owner = {
        "pid": os.getpid(),
        "parentPid": os.getppid(),
        "name": "copilot.exe",
        "startedAt": (datetime.now(timezone.utc) - timedelta(milliseconds=60000)).isoformat().replace("+00:00", "Z"),
    }
    parent = {
        "pid": os.getppid(),
        "name": "github.exe",
        "startedAt": (datetime.now(timezone.utc) - timedelta(milliseconds=120000)).isoformat().replace("+00:00", "Z"),
    }

    def _snapshot() -> dict:
        import time

        return {"at": time.time() * 1000, "processes": [owner, parent]}

    return LocalSource(str(home), _snapshot)


@pytest.mark.asyncio
async def test_reconnect_after_dropped_lease_does_not_invalidate_still_working_session(tmp_path: Path):
    source = await _busy_copilot_home(tmp_path / "home")

    async def _emit(*_a, **_k) -> None:
        return None

    monitor = FamilyMonitor("test-host", _emit, [])
    for id_ in monitor.rows.keys():
        source.tracked.add(id_)

    first = await poll_local({"source": source, "monitor": monitor})
    assert first["healthy"] is True
    assert monitor.rows[SESSION_ID]["state"] == "working", "first poll establishes the session as working"

    # This is the fixed sequence: the reporting lease went null and the
    # collector reconnected, but nothing forces the local monitor to discard
    # what it still knows. A normal poll cycle follows immediately.
    second = await poll_local({"source": source, "monitor": monitor})
    assert second["healthy"] is True
    assert monitor.rows[SESSION_ID]["state"] == "working", (
        "a reconnect with no forced invalidate must not turn a working session unconfirmed"
    )


@pytest.mark.asyncio
async def test_characterization_removed_forced_unhealthy_call_flips_working_session_to_unknown(tmp_path: Path):
    source = await _busy_copilot_home(tmp_path / "home")

    async def _emit(*_a, **_k) -> None:
        return None

    monitor = FamilyMonitor("test-host", _emit, [])
    for id_ in monitor.rows.keys():
        source.tracked.add(id_)

    await poll_local({"source": source, "monitor": monitor})
    assert monitor.rows[SESSION_ID]["state"] == "working"

    # This reproduces, verbatim, the call that watcher.mjs's poll() and
    # server.mjs's localPoll() used to make whenever the lease went null.
    await monitor.update([], {"healthy": False, "reason": "Collector connection changed; rebaselining"})
    assert monitor.rows[SESSION_ID]["state"] == "unknown", (
        "documents why the removed call was destructive: it must never be reintroduced on a mere reconnect"
    )
