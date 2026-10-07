"""Port of test/source.test.mjs -- LocalSource's read-only desktop-app
adapter: ownership detection, hierarchy linkage, and activity tailing.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pymonitor.source import LocalSource, desktop_rows

LOCAL_ID = "11111111-1111-1111-1111-111111111111"
REMOTE_ID = "22222222-2222-2222-2222-222222222222"
CLOUD_ID = "33333333-3333-3333-3333-333333333333"
CLI_ID = "44444444-4444-4444-4444-444444444444"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _iso(ms_offset: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(milliseconds=ms_offset)).isoformat().replace("+00:00", "Z")


def _events_blob(entries: list[dict]) -> str:
    return "\n".join(json.dumps(entry) for entry in entries) + "\n"


@pytest.fixture
def fixture(tmp_path: Path):
    home = tmp_path / "home"
    (home / "session-state").mkdir(parents=True)
    file = home / "data.db"
    conn = sqlite3.connect(str(file))
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
        "INSERT INTO sessions VALUES (?,?,1,0,?,?,NULL)", (LOCAL_ID, "Local work", "local", "project")
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,1,0,?,?,NULL)", (REMOTE_ID, "Remote work", "local", "project")
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?,?,1,0,?,?,NULL)", (CLOUD_ID, "Cloud work", "cloud", "project")
    )
    conn.execute(
        "INSERT INTO workspaces(session_id,host_id,id) VALUES (?,?,?)", (LOCAL_ID, "local", "local-workspace")
    )
    conn.execute(
        "INSERT INTO workspaces(session_id,host_id,id) VALUES (?,?,?)", (REMOTE_ID, "ssh-host", "remote-workspace")
    )
    conn.commit()
    conn.close()

    events = _events_blob(
        [
            {
                "id": "first",
                "type": "session.start",
                "timestamp": _now_iso(),
                "data": {"context": {"cwd": "C:\\demo\\project"}},
            },
            {
                "id": "start",
                "type": "assistant.turn_start",
                "timestamp": _now_iso(),
                "data": {"turnId": "0", "interactionId": "run"},
            },
        ]
    )
    for id_ in (LOCAL_ID, REMOTE_ID, CLOUD_ID, CLI_ID):
        folder = home / "session-state" / id_
        folder.mkdir()
        (folder / f"inuse.{os.getpid()}.lock").write_text(str(os.getpid()), encoding="utf-8")
        (folder / "events.jsonl").write_text(events, encoding="utf-8")

    owner = {
        "pid": os.getpid(),
        "parentPid": os.getppid(),
        "name": "copilot.exe",
        "startedAt": _iso(-60000),
    }
    parent = {"pid": os.getppid(), "name": "github.exe", "startedAt": _iso(-120000)}
    return {"home": str(home), "file": str(file), "owner": owner, "parent": parent}


async def _finish_turn(home: str, id_: str) -> None:
    path = Path(home) / "session-state" / id_ / "events.jsonl"
    extra = _events_blob(
        [
            {"id": "final", "type": "assistant.message", "data": {"turnId": "0", "toolRequests": []}, "timestamp": _now_iso()},
            {"id": "end", "type": "assistant.turn_end", "data": {"turnId": "0"}, "timestamp": _now_iso()},
        ]
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write(extra)


async def test_desktop_adapter_is_read_only_excludes_other_hosts_cloud_and_recognizes_live_owners(fixture):
    home, file, owner, parent = fixture["home"], fixture["file"], fixture["owner"], fixture["parent"]
    before = Path(file).read_bytes()
    source = LocalSource(home, lambda: {"at": time.time() * 1000, "processes": [owner, parent]})
    result = await source.poll()
    assert len(result["samples"]) == 1
    assert result["samples"][0]["id"] == LOCAL_ID
    assert result["samples"][0]["alive"] is True
    assert result["samples"][0]["events"]["activeTurn"] is True
    assert Path(file).read_bytes() == before
    assert len(desktop_rows(file)) == 3


async def test_dead_or_reused_owners_cannot_prove_activity(fixture):
    home, owner, parent = fixture["home"], fixture["owner"], fixture["parent"]
    scenarios = [
        [],
        [{**owner, "startedAt": _iso(60000)}, parent],
        [owner, {**parent, "startedAt": _iso(60000)}],
    ]
    for processes in scenarios:
        result = await LocalSource(home, lambda p=processes: {"at": time.time() * 1000, "processes": p}).poll()
        assert result["samples"][0]["alive"] is False
        assert result["samples"][0]["events"] is None
        assert len(result["issues"]) > 0


async def test_stale_process_evidence_and_schema_failures_do_not_look_like_an_empty_healthy_monitor(fixture):
    home, file = fixture["home"], fixture["file"]
    with pytest.raises(RuntimeError, match="unavailable"):
        await LocalSource(home, lambda: {"at": time.time() * 1000 - 20000, "processes": []}).poll()
    conn = sqlite3.connect(file)
    conn.execute("DROP TABLE workspaces")
    conn.commit()
    conn.close()
    with pytest.raises(Exception):
        await LocalSource(home, lambda: {"at": time.time() * 1000, "processes": []}).poll()


async def test_cli_without_a_desktop_parent_uses_metadata_fallback_and_is_activity_only(fixture):
    home, owner = fixture["home"], fixture["owner"]
    result = await LocalSource(home, lambda: {"at": time.time() * 1000, "processes": [owner]}).poll()
    cli = next(row for row in result["samples"] if row["id"] == CLI_ID)
    assert cli["source"] == "CLI (activity only)"
    assert cli["title"] == f"project - CLI {CLI_ID[:8]}"
    assert cli["busy"] is True
    assert not any(row["id"] in (REMOTE_ID, CLOUD_ID) for row in result["samples"])


async def test_restored_tracking_reconciles_only_known_idle_sessions_without_importing_the_idle_archive(fixture):
    home, file, owner, parent = fixture["home"], fixture["file"], fixture["owner"], fixture["parent"]
    await _finish_turn(home, LOCAL_ID)
    conn = sqlite3.connect(file)
    conn.execute("UPDATE sessions SET is_running=0")
    conn.commit()
    conn.close()
    source = LocalSource(home, lambda: {"at": time.time() * 1000, "processes": [owner, parent]})
    assert len((await source.poll())["samples"]) == 0
    source.tracked.add(LOCAL_ID)
    result = await source.poll()
    assert [row["id"] for row in result["samples"]] == [LOCAL_ID]
    assert result["samples"][0]["busy"] is False
    source.release_idle(set())
    assert len((await source.poll())["samples"]) == 0


async def test_child_only_activity_reads_its_idle_chat_parent_not_unrelated_idle_history(fixture):
    home, file, owner, parent = fixture["home"], fixture["file"], fixture["owner"], fixture["parent"]
    await _finish_turn(home, CLI_ID)
    conn = sqlite3.connect(file)
    conn.execute("INSERT INTO sessions VALUES (?,?,0,0,?,?,NULL)", (CLI_ID, "Chat parent", "local", "general_chat"))
    conn.execute("UPDATE workspaces SET creator_session_id=? WHERE session_id=?", (CLI_ID, LOCAL_ID))
    conn.commit()
    conn.close()
    source = LocalSource(home, lambda: {"at": time.time() * 1000, "processes": [owner, parent]})
    result = await source.poll()
    assert sorted(row["id"] for row in result["samples"]) == sorted([LOCAL_ID, CLI_ID])
    child = next(row for row in result["samples"] if row["id"] == LOCAL_ID)
    chat = next(row for row in result["samples"] if row["id"] == CLI_ID)
    assert child["parentId"] == CLI_ID
    assert chat["contextOnly"] is True
    assert chat["alive"] is True
    assert chat["busy"] is False


async def test_missing_parent_link_does_not_hide_reliable_local_child_execution(fixture):
    home, file, owner, parent = fixture["home"], fixture["file"], fixture["owner"], fixture["parent"]
    conn = sqlite3.connect(file)
    conn.execute("INSERT INTO workspace_parent_links VALUES (?,?)", ("local-workspace", "missing-parent"))
    conn.commit()
    conn.close()
    result = await LocalSource(home, lambda: {"at": time.time() * 1000, "processes": [owner, parent]}).poll()
    child = next(row for row in result["samples"] if row["id"] == LOCAL_ID)
    assert child["alive"] is True
    assert child["events"]["activeTurn"] is True
    assert child.get("readError") is None
    assert re.search("missing", child["hierarchyIssue"])
    missing_parent = next(row for row in result["samples"] if row["id"] == "missing-parent")
    assert re.search("missing", missing_parent["readError"])
    assert len(result["issues"]) > 0


async def test_additional_dormant_nested_children_provide_names_only_without_opening_their_event_files(fixture):
    home, file, owner, parent = fixture["home"], fixture["file"], fixture["owner"], fixture["parent"]
    conn = sqlite3.connect(file)
    conn.execute("UPDATE sessions SET is_running=0 WHERE id!=?", (LOCAL_ID,))
    conn.execute("INSERT INTO workspace_parent_links VALUES (?,?)", ("remote-workspace", "local-workspace"))
    conn.execute(
        "INSERT INTO workspaces(session_id,host_id,id) VALUES (?,?,?)", (CLOUD_ID, "remote-host", "grand-workspace")
    )
    conn.execute("INSERT INTO workspace_parent_links VALUES (?,?)", ("grand-workspace", "remote-workspace"))
    conn.commit()
    conn.close()
    source = LocalSource(home, lambda: {"at": time.time() * 1000, "processes": [owner, parent]})
    result = await source.poll()
    assert [row["id"] for row in result["samples"]] == [LOCAL_ID]
    assert next(row for row in result["relatives"] if row["id"] == REMOTE_ID)["title"] == "Remote work"
    assert next(row for row in result["relatives"] if row["id"] == CLOUD_ID)["parentId"] == REMOTE_ID
    assert list(source.tails.keys()) == [LOCAL_ID]


async def test_idle_persisted_flag_discovers_current_root_execution_but_never_idle_live_processes(fixture):
    home, file, owner, parent = fixture["home"], fixture["file"], fixture["owner"], fixture["parent"]
    conn = sqlite3.connect(file)
    conn.execute("UPDATE sessions SET is_running=0")
    conn.commit()
    conn.close()
    source = LocalSource(home, lambda: {"at": time.time() * 1000, "processes": [owner, parent]})
    result = await source.poll()
    assert next(row for row in result["samples"] if row["id"] == LOCAL_ID)["busy"] is True
    await _finish_turn(home, LOCAL_ID)
    fresh = LocalSource(home, lambda: {"at": time.time() * 1000, "processes": [owner, parent]})
    assert len((await fresh.poll())["samples"]) == 0


async def test_attached_background_work_with_is_running_0_is_discovered_under_its_canonical_idle_parent(fixture):
    home, file, owner, parent = fixture["home"], fixture["file"], fixture["owner"], fixture["parent"]
    conn = sqlite3.connect(file)
    conn.execute("UPDATE sessions SET is_running=0")
    conn.execute("INSERT INTO sessions VALUES (?,?,0,0,?,?,NULL)", (CLI_ID, "Idle root", "local", "general_chat"))
    conn.execute("UPDATE workspaces SET creator_session_id=? WHERE session_id=?", (CLI_ID, LOCAL_ID))
    conn.commit()
    conn.close()
    await _finish_turn(home, CLI_ID)
    background = _events_blob(
        [
            {
                "id": "launch",
                "type": "tool.execution_start",
                "data": {"toolName": "powershell", "toolCallId": "shell", "arguments": {}},
                "timestamp": _now_iso(),
            },
            {
                "id": "running",
                "type": "tool.execution_complete",
                "data": {
                    "toolCallId": "shell",
                    "success": True,
                    "result": {
                        "content": "<command with shellId: shell-a is still running after 180 seconds. No output yet.>"
                    },
                },
                "timestamp": _now_iso(),
            },
        ]
    )
    with (Path(home) / "session-state" / LOCAL_ID / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(background)
    await _finish_turn(home, LOCAL_ID)
    source = LocalSource(home, lambda: {"at": time.time() * 1000, "processes": [owner, parent]})
    result = await source.poll()
    local_row = next(row for row in result["samples"] if row["id"] == LOCAL_ID)
    assert local_row["busy"] is True
    assert local_row["events"]["backgroundCount"] == 1
    assert local_row["events"]["terminal"] is None
    cli_row = next(row for row in result["samples"] if row["id"] == CLI_ID)
    assert cli_row["busy"] is False
    assert cli_row["contextOnly"] is True
    source.release_idle({LOCAL_ID, CLI_ID})
    result2 = await source.poll()
    assert next(row for row in result2["samples"] if row["id"] == LOCAL_ID)["busy"] is True


async def test_sdk_discover_seam_surfaces_cli_only_sessions_found_by_the_injected_callable(fixture):
    """Exercises the LocalSource(sdk_discover=...) injection seam directly,
    standing in for github-copilot-sdk's Client.list_sessions() (see
    docs/porting-notes.md "SDK discovery"). Bypasses the autouse directory-scan
    fixture to prove the seam itself -- not just the merge logic -- is wired.
    """
    home, owner = fixture["home"], fixture["owner"]
    calls = 0

    async def fake_sdk_discover() -> list[str]:
        nonlocal calls
        calls += 1
        return [CLI_ID, "not-a-uuid"]

    source = LocalSource(
        home,
        lambda: {"at": time.time() * 1000, "processes": [owner]},
        sdk_discover=fake_sdk_discover,
    )
    result = await source.poll()
    cli = next(row for row in result["samples"] if row["id"] == CLI_ID)
    assert cli["source"] == "CLI (activity only)"
    assert calls == 1
    # Non-UUID ids returned by a (possibly misbehaving) SDK are filtered out.
    assert not any(row["id"] == "not-a-uuid" for row in result["samples"])


async def test_sdk_discover_failure_is_reported_as_an_issue_and_does_not_crash_poll(fixture):
    home, owner, parent = fixture["home"], fixture["owner"], fixture["parent"]

    async def failing_sdk_discover() -> list[str]:
        raise RuntimeError("runtime process exited")

    source = LocalSource(
        home,
        lambda: {"at": time.time() * 1000, "processes": [owner, parent]},
        sdk_discover=failing_sdk_discover,
    )
    result = await source.poll()
    assert any("CLI session discovery unavailable" in issue for issue in result["issues"])
    # discovered_at still advances so a broken SDK can't cause a tight retry loop.
    assert source.discovered_at > 0


async def test_historical_open_background_work_from_a_different_owner_lifetime_is_unconfirmed_never_working(fixture):
    home, file, owner, parent = fixture["home"], fixture["file"], fixture["owner"], fixture["parent"]
    conn = sqlite3.connect(file)
    conn.execute("UPDATE sessions SET is_running=0")
    conn.commit()
    conn.close()
    timestamp = (
        datetime.fromisoformat(owner["startedAt"].replace("Z", "+00:00")) - timedelta(milliseconds=10000)
    ).isoformat().replace("+00:00", "Z")
    background = _events_blob(
        [
            {
                "id": "launch",
                "type": "tool.execution_start",
                "data": {"toolName": "powershell", "toolCallId": "old", "arguments": {}},
                "timestamp": timestamp,
            },
            {
                "id": "pending",
                "type": "tool.execution_complete",
                "data": {
                    "toolCallId": "old",
                    "success": True,
                    "result": {"content": "<command started in background with shellId: old>"},
                },
                "timestamp": timestamp,
            },
        ]
    )
    with (Path(home) / "session-state" / LOCAL_ID / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(background)
    await _finish_turn(home, LOCAL_ID)
    result = await LocalSource(home, lambda: {"at": time.time() * 1000, "processes": [owner, parent]}).poll()
    row = next(row for row in result["samples"] if row["id"] == LOCAL_ID)
    assert row["busy"] is False
    assert re.search("ownership", row["activityUnconfirmed"])


async def test_background_work_with_no_recent_activity_goes_stale_to_unconfirmed_not_working(fixture):
    """A background shell the agent never re-checks must not read "Working"
    forever just because its owning process is still alive. Repro for the
    real-world "Unchanged save progress" session: backgroundAt is after the
    owner's startedAt (so it isn't the pre-existing owner-predates case), but
    is older than the staleness window with nothing newer to corroborate it.
    """
    home, file = fixture["home"], fixture["file"]
    # Both owner and parent need to predate the stale-but-not-ancient event, and
    # parent must still predate owner (ownership-matching invariant in owner()).
    owner = {**fixture["owner"], "startedAt": _iso(-2_000_000)}
    parent = {**fixture["parent"], "startedAt": _iso(-2_100_000)}
    conn = sqlite3.connect(file)
    conn.execute("UPDATE sessions SET is_running=0")
    conn.commit()
    conn.close()
    stale_at = _iso(-1_000_000)  # after owner start, but past the 15-minute staleness window
    background = _events_blob(
        [
            {
                "id": "launch",
                "type": "tool.execution_start",
                "data": {"toolName": "powershell", "toolCallId": "shell", "arguments": {}},
                "timestamp": stale_at,
            },
            {
                "id": "running",
                "type": "tool.execution_complete",
                "data": {
                    "toolCallId": "shell",
                    "success": True,
                    "result": {
                        "content": "<command with shellId: shell-a is still running after 180 seconds. No output yet.>"
                    },
                },
                "timestamp": stale_at,
            },
        ]
    )
    with (Path(home) / "session-state" / LOCAL_ID / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(background)
    await _finish_turn(home, LOCAL_ID)
    result = await LocalSource(home, lambda: {"at": time.time() * 1000, "processes": [owner, parent]}).poll()
    row = next(row for row in result["samples"] if row["id"] == LOCAL_ID)
    assert row["events"]["backgroundCount"] == 1
    assert row["busy"] is False
    assert re.search("ownership", row["activityUnconfirmed"])


async def test_background_work_within_staleness_window_still_reads_working(fixture):
    """Counterpart to the staleness test above: a background shell observed
    recently (well within the 15-minute window) must keep reading "Working",
    proving this isn't a blanket suppression of background busy signals.
    """
    home, file, owner, parent = fixture["home"], fixture["file"], fixture["owner"], fixture["parent"]
    conn = sqlite3.connect(file)
    conn.execute("UPDATE sessions SET is_running=0")
    conn.commit()
    conn.close()
    background = _events_blob(
        [
            {
                "id": "launch",
                "type": "tool.execution_start",
                "data": {"toolName": "powershell", "toolCallId": "shell", "arguments": {}},
                "timestamp": _now_iso(),
            },
            {
                "id": "running",
                "type": "tool.execution_complete",
                "data": {
                    "toolCallId": "shell",
                    "success": True,
                    "result": {
                        "content": "<command with shellId: shell-a is still running after 180 seconds. No output yet.>"
                    },
                },
                "timestamp": _now_iso(),
            },
        ]
    )
    with (Path(home) / "session-state" / LOCAL_ID / "events.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(background)
    await _finish_turn(home, LOCAL_ID)
    result = await LocalSource(home, lambda: {"at": time.time() * 1000, "processes": [owner, parent]}).poll()
    row = next(row for row in result["samples"] if row["id"] == LOCAL_ID)
    assert row["busy"] is True
    assert row["activityUnconfirmed"] is None
