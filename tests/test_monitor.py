"""Port of test/monitor.test.mjs -- the core status state machine regression suite.

These tests are the primary guard against regressing the six hard-won bug
fixes described in docs/porting-notes.md (unconfirmed-after-finished,
family infection, zombie rows, gap/offline handling, reconnect-after-lease,
and the historical-vs-live completion distinction). Keep this file a
faithful, traceable port of the original -- do not "clean up" or
restructure the test bodies relative to the .mjs source.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest

from pymonitor.engine import Ledger, MonitorEngine, SessionStore
from pymonitor.events import EventState, JsonlTail

_serial = 0


def _iso(ms: float) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def event(type_: str, data: dict | None = None, extra: dict | None = None) -> dict[str, Any]:
    global _serial
    _serial += 1
    return {
        "id": f"event-{_serial}",
        "type": type_,
        "data": data or {},
        "timestamp": _iso(1790000000000 + _serial),
        **(extra or {}),
    }


def start(interaction_id: str = "run-a", turn_id: str = "0") -> dict[str, Any]:
    return event("assistant.turn_start", {"interactionId": interaction_id, "turnId": turn_id})


def message(tools: list | None = None, phase: str = "final_answer", turn_id: str = "0") -> dict[str, Any]:
    return event(
        "assistant.message",
        {"turnId": turn_id, "phase": phase, "toolRequests": tools or [], "content": "PRIVATE TRANSCRIPT SENTINEL"},
    )


def end(turn_id: str = "0") -> dict[str, Any]:
    return event("assistant.turn_end", {"turnId": turn_id})


def state(*events: dict[str, Any]) -> EventState:
    result = EventState()
    for e in events:
        result.accept(e)
    return result


def sample(s: EventState, overrides: dict | None = None) -> dict[str, Any]:
    base = {
        "id": "session-a", "title": "Readable title", "source": "Copilot desktop", "busy": True,
        "interrupted": False, "alive": True, "owner": "pid:createdAt", "events": s.snapshot(),
    }
    base.update(overrides or {})
    return base


@dataclass
class Rig:
    engine: MonitorEngine
    alerts: list[dict[str, Any]]


def rig(retained: list | None = None, process_started_at_ms: float | None = None) -> Rig:
    alerts: list[dict[str, Any]] = []
    seen: set[str] = set()

    async def emit(key: str, alert: dict[str, Any]) -> None:
        if key not in seen:
            alerts.append(alert)
            seen.add(key)

    engine = MonitorEngine("TEST-MACHINE", emit, retained or [], process_started_at_ms)
    return Rig(engine, alerts)


async def test_historical_completed_sessions_do_not_notify_observed_live_run_finishes_once():
    r = rig()
    old = state(start(), message(), end())
    view = await r.engine.update([sample(old, {"busy": False})], now=1000)
    assert len(view["active"]) == 0
    assert len(r.alerts) == 0
    current = state(start("run-b"))
    active = await r.engine.update([sample(current)], now=2000)
    assert len(active["active"]) == 1
    assert active["active"][0]["machine"] == "TEST-MACHINE"
    current.accept(message())
    current.accept(end())
    done = await r.engine.update([sample(current, {"busy": False})], now=3000)
    assert len(done["active"]) == 0
    assert len(done["sessions"]) == 1
    assert done["sessions"][0]["state"] == "finished"
    assert done["sessions"][0]["finishedAt"] == _iso(3000)
    assert done["sessions"][0]["lastResponseAt"] == current.last_response_at
    assert len(r.alerts) == 1
    assert r.alerts[0]["kind"] == "finished"
    assert re.search("does not mean", r.alerts[0]["message"])
    await r.engine.update([sample(current, {"busy": False})], now=4000)
    assert len(r.alerts) == 1
    assert r.engine.snapshot()["sessions"][0]["finishedAt"] == _iso(3000)


def test_response_time_comes_only_from_root_assistant_messages_survives_new_runs_and_replay():
    s = state(start())
    assert s.snapshot()["lastResponseAt"] is None
    first = message([], "commentary")
    s.accept(first)
    s.accept(event("tool.execution_complete", {"toolCallId": "t"}))
    s.accept(event("assistant.message", {}, {"agentId": "nested"}))
    s.accept(event("assistant.message", {"parentToolCallId": "nested"}))
    s.accept(event("session.resume"))
    s.accept(start("new-run"))
    earlier = datetime.fromtimestamp(datetime.fromisoformat(first["timestamp"].replace("Z", "+00:00")).timestamp() - 0.1, tz=timezone.utc)
    s.accept(event("assistant.message", {}, {"timestamp": earlier.isoformat().replace("+00:00", "Z")}))
    assert s.snapshot()["lastResponseAt"] == first["timestamp"]
    latest = message()
    s.accept(latest)
    assert s.snapshot()["lastResponseAt"] == latest["timestamp"]
    assert "PRIVATE TRANSCRIPT" not in json.dumps(s.snapshot())


async def test_retained_rows_sort_by_response_not_working_first_completion_tool_or_polling_time():
    r = rig()
    older_response = message()
    newer_response = message()
    older = state(start("older"), older_response)
    newer = state(start("newer"), newer_response)
    await r.engine.update([sample(older, {"id": "older"}), sample(newer, {"id": "newer"})], now=1000)
    newer.accept(end())
    older.accept(event("tool.execution_start", {"toolCallId": "t", "toolName": "powershell"}))
    view = await r.engine.update(
        [sample(older, {"id": "older"}), sample(newer, {"id": "newer", "busy": False})], now=2000
    )
    assert [(row["id"], row["state"]) for row in view["sessions"]] == [("newer", "finished"), ("older", "working")]
    finished_at = view["sessions"][0]["finishedAt"]
    older.accept(event("tool.execution_complete", {"toolCallId": "t"}))
    view = await r.engine.update([sample(older, {"id": "older"})], now=3000)
    assert [row["id"] for row in view["sessions"]] == ["newer", "older"]
    assert view["sessions"][0]["finishedAt"] == finished_at
    older.accept(message([], "commentary"))
    view = await r.engine.update([sample(older, {"id": "older"})], now=4000)
    assert [row["id"] for row in view["sessions"]] == ["older", "newer"]


async def test_missing_response_uses_stable_first_observation_never_invented_or_repeated():
    r = rig()
    first, second = state(start("first")), state(start("second"))
    await r.engine.update([sample(first, {"id": "first"})], now=1000)
    await r.engine.update([sample(first, {"id": "first"}), sample(second, {"id": "second"})], now=2000)
    view = await r.engine.update([sample(first, {"id": "first"}), sample(second, {"id": "second"})], now=3000)
    assert [row["id"] for row in view["sessions"]] == ["second", "first"]
    assert view["sessions"][1]["lastResponseAt"] is None
    assert view["sessions"][1]["firstObservedAt"] == _iso(1000)


async def test_new_and_resumed_runs_replace_one_retained_row_without_duplicate_alerts():
    r = rig()
    s = state(start())
    await r.engine.update([sample(s)], now=1000)
    s.accept(message())
    s.accept(end())
    await r.engine.update([sample(s, {"busy": False})], now=2000)
    old_response = s.last_response_at
    for index, resume in enumerate([False, True]):
        if resume:
            s.accept(event("session.resume"))
        s.accept(start(f"next-{index}"))
        view = await r.engine.update([sample(s)], now=3000 + index * 2000)
        assert len(view["sessions"]) == 1
        assert view["sessions"][0]["state"] == "working"
        assert view["sessions"][0]["finishedAt"] is None
        if not resume:
            assert view["sessions"][0]["lastResponseAt"] == old_response
        s.accept(message())
        s.accept(end())
        view = await r.engine.update([sample(s, {"busy": False})], now=4000 + index * 2000)
        assert len(view["sessions"]) == 1
        assert view["sessions"][0]["state"] == "finished"
        assert view["sessions"][0]["firstObservedAt"] == _iso(1000)
    assert len([a for a in r.alerts if a["kind"] == "finished"]) == 3


async def test_finished_rows_persist_through_outages_and_never_reimport_old_idle_sessions():
    r = rig()
    s = state(start())
    await r.engine.update([sample(s)], now=1000)
    s.accept(message())
    s.accept(end())
    finished = (await r.engine.update([sample(s, {"busy": False})], now=2000))["sessions"][0]
    await r.engine.update([], healthy=False, now=3000)
    await r.engine.update([], now=86400000)
    old = state(start("unobserved"), message(), end())
    view = await r.engine.update([sample(old, {"id": "unobserved", "busy": False})], now=86401000)
    assert view["sessions"] == [finished]
    assert len(r.alerts) == 1


async def test_metadata_only_store_retains_completed_rows_restart_never_promotes_downtime(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    assert await store.load() == []
    r = rig()
    complete, running = state(start("complete")), state(start("running"))
    await r.engine.update([sample(complete), sample(running, {"id": "running"})], now=1000)
    complete.accept(message())
    complete.accept(end())
    view = await r.engine.update(
        [sample(complete, {"busy": False}), sample(running, {"id": "running"})], now=2000
    )
    await store.save([{**row, "transcript": "PRIVATE TRANSCRIPT SENTINEL"} for row in view["sessions"]])
    disk = (tmp_path / "sessions.json").read_text(encoding="utf-8")
    assert "PRIVATE" not in disk
    restored = await SessionStore(store.file).load()
    restarted = rig(restored)
    assert restarted.engine.rows["running"]["state"] == "unknown"
    assert restarted.engine.rows["session-a"]["state"] == "finished"
    running.accept(message())
    running.accept(end())
    after = await restarted.engine.update(
        [sample(complete, {"busy": False}), sample(running, {"id": "running", "busy": False})], now=10000
    )
    assert len(after["sessions"]) == 2
    assert restarted.engine.rows["running"]["state"] == "unknown"
    assert restarted.engine.rows["session-a"]["finishedAt"] == _iso(2000)
    assert len(restarted.alerts) == 0
    running.accept(start("fresh"))
    await restarted.engine.update([sample(running, {"id": "running"})], now=11000)
    running.accept(message())
    running.accept(end())
    await restarted.engine.update([sample(running, {"id": "running", "busy": False})], now=12000)
    assert len(restarted.alerts) == 1


async def test_finished_row_restored_from_before_this_engine_started_is_unconfirmed_until_reobserved():
    # A cold host restart loading a session that finished days ago (e.g. from
    # disk, never seen by this process before) must not surface as a fresh,
    # legitimate "finished" row -- this is the MonitorEngine-level counterpart
    # to FamilyMonitor's own restore-staleness check, which only covers rows
    # FamilyMonitor itself previously persisted, not rows supplied fresh here.
    stale = {
        "id": "stale-session", "title": "Old work", "machine": "TEST-MACHINE", "source": "Copilot desktop",
        "state": "finished", "detail": "Completed two days ago", "activity": "Agent running", "runId": "run-old",
        "firstObservedAt": _iso(1000), "startedAt": _iso(1000), "lastEventAt": _iso(1000),
        "lastResponseAt": _iso(1000), "finishedAt": _iso(1000),
        "parentId": None, "hierarchyIssue": None, "contextOnly": False, "lastAlert": None,
    }
    r = rig([stale], process_started_at_ms=200000)
    assert r.engine.rows["stale-session"]["state"] == "unknown"
    assert r.engine.rows["stale-session"]["finishedAt"] is None
    view = await r.engine.update([], now=300000)
    assert view["sessions"][0]["state"] == "unknown"
    assert len(r.alerts) == 0
    # A row that finished AFTER this engine started must be left alone.
    fresh = {**stale, "id": "fresh-session", "finishedAt": _iso(250000)}
    r2 = rig([fresh], process_started_at_ms=200000)
    assert r2.engine.rows["fresh-session"]["state"] == "finished"
    assert r2.engine.rows["fresh-session"]["finishedAt"] == _iso(250000)


async def test_saved_waiting_state_is_unconfirmed_after_restart_unless_fresh_evidence_supports_it():
    r = rig()
    s = state(start())
    await r.engine.update([sample(s)], now=1000)
    s.accept(event("user_input.requested", {"requestId": "input"}))
    waiting = await r.engine.update([sample(s)], now=2000)
    assert waiting["sessions"][0]["state"] == "waiting"
    restarted = rig(waiting["sessions"])
    assert restarted.engine.snapshot()["sessions"][0]["state"] == "unknown"
    idle = await restarted.engine.update([sample(s, {"busy": False})], now=3000)
    assert idle["sessions"][0]["state"] == "unknown"
    fresh = await restarted.engine.update([sample(s)], now=4000)
    assert fresh["sessions"][0]["state"] == "waiting"
    assert len(restarted.alerts) == 0
    missing = await restarted.engine.update([], now=5000)
    assert missing["sessions"][0]["state"] == "unknown"


async def test_retained_session_persistence_fails_visibly_for_corruption_serializes_writes(tmp_path):
    import asyncio

    file = str(tmp_path / "sessions.json")
    r = rig()
    rows = (await r.engine.update([sample(state(start()))], now=1000))["sessions"]
    store = SessionStore(file)
    await asyncio.gather(
        *(store.save([{**row, "title": f"Title {i}"} for row in rows]) for i in range(8))
    )
    loaded = await SessionStore(file).load()
    assert loaded[0]["title"] == "Title 7"
    bad_payloads = [
        "corrupt",
        json.dumps({"version": 1, "sessions": [rows[0], rows[0]]}),
        json.dumps({"version": 1, "sessions": [{**rows[0], "state": "finished"}]}),
        json.dumps({"version": 1, "sessions": [{**rows[0], "lastResponseAt": "yesterday"}]}),
    ]
    for data in bad_payloads:
        (tmp_path / "sessions.json").write_text(data, encoding="utf-8")
        with pytest.raises(Exception):
            await SessionStore(file).load()


async def test_model_tool_iteration_end_is_never_whole_run_completion():
    r = rig()
    s = state(start())
    await r.engine.update([sample(s)], now=1000)
    s.accept(message([{"name": "powershell"}], "commentary"))
    s.accept(event("tool.execution_start", {"toolCallId": "t", "toolName": "powershell"}))
    s.accept(event("tool.execution_complete", {"toolCallId": "t", "success": True, "shellExecution": {"exitCode": 0}}))
    s.accept(end())
    assert s.terminal is None
    await r.engine.update([sample(s)], now=2000)
    s.accept(start("run-a", "1"))
    assert len((await r.engine.update([sample(s)], now=3000))["active"]) == 1
    assert len(r.alerts) == 0


def test_unphased_final_messages_work_but_intermediate_chunks_and_commentary_do_not():
    final = event("assistant.message", {"turnId": "0", "toolRequests": []})
    assert state(start(), final, end()).terminal
    assert state(start(), message([], "commentary"), end()).terminal is None
    assert state(
        start(),
        event("assistant.message", {"turnId": "0", "toolRequests": [], "chunkCount": 2, "chunkIndex": 0}),
        end(),
    ).terminal is None


async def test_no_recent_output_is_not_an_idle_or_completion_signal():
    r = rig()
    s = state(start())
    now = 0
    while now <= 120000:
        assert len((await r.engine.update([sample(s)], now=now))["active"]) == 1
        now += 5000
    assert len(r.alerts) == 0


@pytest.mark.parametrize(
    "prefix,label",
    [
        ("permission", "Permission needed"),
        ("user_input", "Input needed"),
        ("exit_plan_mode", "Plan approval needed"),
        ("elicitation", "Input needed"),
    ],
)
async def test_gate_prefix_is_distinct_from_working_and_completion_then_resumes(prefix, label):
    r = rig()
    s = state(start())
    await r.engine.update([sample(s)], now=1000)
    s.accept(event(f"{prefix}.requested", {"requestId": "request", "toolCallId": "tool"}))
    view = await r.engine.update([sample(s)], now=2000)
    assert len(view["active"]) == 0
    assert view["attention"][0]["detail"] == label
    assert r.alerts[0]["kind"] == "waiting"
    await r.engine.update([sample(s)], now=3000)
    assert len(r.alerts) == 1
    s.accept(event("tool.execution_complete", {"toolCallId": "tool"}))
    view = await r.engine.update([sample(s)], now=4000)
    assert len(view["active"]) == 1
    assert len(view["attention"]) == 0


def test_ephemeral_wait_completion_can_be_recovered_from_the_next_root_turn():
    s = state(start(), event("permission.requested", {"requestId": "p"}), start("run-a", "1"))
    assert len(s.gates) == 0


def test_resolved_by_hook_permission_never_shows_waiting():
    s = state(start(), event("permission.requested", {"requestId": "p", "resolvedByHook": True}))
    assert len(s.gates) == 0


def test_desktop_ask_user_external_tools_do_not_look_like_active_execution():
    s = state(
        start(),
        event("tool.execution_start", {"toolCallId": "q", "toolName": "functions.ask_user"}),
        event("external_tool.requested", {"toolCallId": "q", "requestId": "r", "toolName": "ask_user"}),
    )
    assert s.snapshot()["waiting"]["kind"] == "Input needed"
    s.accept(event("external_tool.completed", {"requestId": "r"}))
    s.accept(event("tool.execution_complete", {"toolCallId": "q"}))
    assert s.snapshot()["waiting"] is None


@pytest.mark.parametrize("event_type", ["abort", "session.error"])
async def test_abort_or_session_error_cannot_become_a_successful_completion(event_type):
    r = rig()
    s = state(start())
    await r.engine.update([sample(s)], now=1000)
    s.accept(event(event_type))
    s.accept(message())
    s.accept(end())
    view = await r.engine.update([sample(s, {"busy": False})], now=2000)
    assert len(view["active"]) == 0
    assert r.alerts[0]["kind"] == "error"
    assert not any(a["kind"] == "finished" for a in r.alerts)


@pytest.mark.parametrize(
    "change",
    [
        {"alive": False},
        {"owner": "reused-pid:new-createdAt"},
        {"readError": "Reader unavailable"},
        {"interrupted": True},
    ],
)
async def test_failure_cannot_imply_success(change):
    r = rig()
    s = state(start())
    await r.engine.update([sample(s)], now=1000)
    s.accept(message())
    s.accept(end())
    view = await r.engine.update([sample(s, {"busy": False, **change})], now=2000)
    assert len(view["active"]) == 0
    assert not any(a["kind"] == "finished" for a in r.alerts)


async def test_source_disconnection_and_sleep_discard_completion_authority_reconnect_baselines():
    for outage in ({"healthy": False, "now": 2000}, {"now": 90000}):
        r = rig()
        s = state(start())
        await r.engine.update([sample(s)], now=1000)
        s.accept(message())
        s.accept(end())
        await r.engine.update([sample(s, {"busy": False})], **outage)
        await r.engine.update([sample(s, {"busy": False})], now=outage["now"] + 1000)
        assert len([a for a in r.alerts if a["kind"] == "finished"]) == 0
        assert r.alerts[0]["kind"] == "warning"


async def test_missing_session_shutdown_and_rotation_all_fail_closed():
    for kind in ("missing", "shutdown", "rotation"):
        r = rig()
        s = state(start())
        await r.engine.update([sample(s)], now=1000)
        s.accept(message())
        s.accept(end())
        if kind == "shutdown":
            s.accept(event("session.shutdown"))
        row = sample(s, {"busy": False})
        if kind == "rotation":
            row["events"]["replaced"] = True
        await r.engine.update([] if kind == "missing" else [row], now=2000)
        assert not any(a["kind"] == "finished" for a in r.alerts)


async def test_database_idle_before_final_jsonl_flush_does_not_lose_completion_evidence():
    r = rig()
    s = state(start())
    await r.engine.update([sample(s)], now=1000)
    pending = await r.engine.update([sample(s, {"busy": False})], now=2000)
    assert len(pending["active"]) == 0
    assert len(r.alerts) == 0
    s.accept(message())
    s.accept(end())
    await r.engine.update([sample(s, {"busy": False})], now=3000)
    assert r.alerts[0]["kind"] == "finished"


async def test_partial_jsonl_never_confirms_completion():
    r = rig()
    s = state(start())
    await r.engine.update([sample(s)], now=1000)
    s.accept(message())
    s.accept(end())
    row = sample(s, {"busy": False})
    row["events"]["partial"] = True
    assert len((await r.engine.update([row], now=2000))["active"]) == 0
    assert len(r.alerts) == 0


async def test_standalone_cli_model_completion_claims_full_run_completion_same_as_desktop():
    r = rig()
    s = state(start())
    assert len((await r.engine.update([sample(s, {"source": "CLI (activity only)"})], now=1000))["active"]) == 1
    s.accept(message())
    s.accept(end())
    view = await r.engine.update([sample(s, {"source": "CLI (activity only)", "busy": False})], now=2000)
    assert len(view["active"]) == 0
    assert view["sessions"][0]["state"] == "finished"
    assert len(r.alerts) == 1
    assert r.alerts[0]["kind"] == "finished"


def test_nested_subagent_events_cannot_finish_root_replay_event_ids_are_ignored():
    e = start()
    s = state(e, e)
    root_run = s.run_id
    s.accept(event("assistant.turn_start", {"turnId": "child"}, {"agentId": "child"}))
    s.accept(event("session.task_complete", {"success": True}, {"agentId": "child"}))
    assert s.run_id == root_run
    assert s.terminal is None
    assert "PRIVATE" not in json.dumps(s.snapshot())


async def test_task_complete_is_an_explicit_marker_but_desktop_must_actually_stop_running():
    r = rig()
    s = state(start())
    assert len((await r.engine.update([sample(s)], now=1000))["active"]) == 1
    s.accept(event("session.task_complete", {"success": True}))
    await r.engine.update([sample(s)], now=1500)
    assert len(r.alerts) == 0
    await r.engine.update([sample(s, {"busy": False})], now=2000)
    assert r.alerts[0]["kind"] == "finished"


async def test_already_terminal_stale_log_cannot_confirm_a_newly_observed_running_flag_clearing():
    r = rig()
    s = state(start(), message(), end())
    await r.engine.update([sample(s)], now=1000)
    await r.engine.update([sample(s)], now=1500)
    view = await r.engine.update([sample(s, {"busy": False})], now=2000)
    assert len(view["active"]) == 0
    assert view["attention"][0]["state"] == "unknown"
    assert len(r.alerts) == 0


async def test_restart_baselines_historical_completion_and_ledger_deduplicates_delivery(tmp_path):
    file = str(tmp_path / "ledger.json")
    ledger = Ledger(file)
    await ledger.load()
    assert await ledger.claim("same-run:finished") is True
    restarted = Ledger(file)
    await restarted.load()
    assert await restarted.claim("same-run:finished") is False
    persisted = (tmp_path / "ledger.json").read_text(encoding="utf-8")
    assert "same-run" not in persisted
    (tmp_path / "ledger.json").write_text("corrupt", encoding="utf-8")
    with pytest.raises(Exception):
        await Ledger(file).load()


async def test_concurrent_notification_claims_are_serialized_and_persisted_exactly_once(tmp_path):
    import asyncio

    ledger = Ledger(str(tmp_path / "notifications.json"))
    await ledger.load()
    results = await asyncio.gather(*(ledger.claim(f"run-{n % 20}") for n in range(40)))
    assert len([r for r in results if r]) == 20
    after_restart = Ledger(ledger.file)
    await after_restart.load()
    assert len(after_restart.keys) == 20
    assert await after_restart.claim("run-1") is False


async def test_tail_handles_partial_utf8_json_writes_without_retaining_transcript_content(tmp_path):
    file = tmp_path / "events.jsonl"
    first = start()
    msg = message()
    msg_with_snowman = {**msg, "data": {**msg["data"], "content": "private \u2603"}}
    line = (json.dumps(msg_with_snowman, ensure_ascii=False) + "\n").encode("utf-8")
    file.write_text(json.dumps(first) + "\n", encoding="utf-8")
    tail = JsonlTail(str(file))
    first_read = await tail.read()
    assert first_read["runId"] == first["id"]
    snowman_bytes = "\u2603".encode("utf-8")
    split = line.index(snowman_bytes) + 1
    with open(file, "ab") as handle:
        handle.write(line[:split])
    partial_read = await tail.read()
    assert partial_read["partial"] is True
    with open(file, "ab") as handle:
        handle.write(line[split:] + (json.dumps(end()) + "\n").encode("utf-8"))
    result = await tail.read()
    assert result["terminal"]
    assert result["partial"] is False
    assert "private" not in json.dumps(result)
    assert len(tail.pending) == 0


async def test_tail_detects_replacement_truncation_and_same_inode_rewrite_regrowth(tmp_path):
    import os as _os

    file = tmp_path / "events.jsonl"
    file.write_text(json.dumps(start()) + "\n", encoding="utf-8")
    tail = JsonlTail(str(file))
    await tail.read()
    old_file = tmp_path / "old.jsonl"
    _os.rename(file, old_file)
    file.write_text(json.dumps(start("new")) + "\n", encoding="utf-8")
    assert (await tail.read())["replaced"] is True
    file.write_text("", encoding="utf-8")
    assert (await tail.read())["replaced"] is True
    file.write_text(json.dumps(start()) + "\n", encoding="utf-8")
    await tail.read()
    file.write_text(
        "\n".join(json.dumps(e) for e in (start("different"), message(), end())) + "\n", encoding="utf-8"
    )
    assert (await tail.read())["replaced"] is True


async def test_malformed_complete_lines_fail_closed_and_recover_only_after_repair(tmp_path):
    file = tmp_path / "events.jsonl"
    file.write_text(json.dumps(start()) + "\n{broken}\n", encoding="utf-8")
    tail = JsonlTail(str(file))
    with pytest.raises(Exception):
        await tail.read()
    assert tail.state.run_id is None
    file.write_text(json.dumps(start("repaired")) + "\n", encoding="utf-8")
    assert (await tail.read())["runId"]
