"""Port of test/background.test.mjs -- background/detached shell work tracking.

Covers the "backgroundCount"/"backgroundUnconfirmed" bookkeeping in
EventState (native shell status envelope parsing) plus FamilyMonitor's
family-level completion gating so attached background work never gets
misreported as finished.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import pytest

from pymonitor.events import EventState
from pymonitor.families import FamilyMonitor

_sequence = 0


def event(type_: str, data: dict | None = None, extra: dict | None = None) -> dict[str, Any]:
    global _sequence
    _sequence += 1
    return {
        "id": f"background-{_sequence}",
        "type": type_,
        "data": data or {},
        "timestamp": datetime.fromtimestamp((1790980000000 + _sequence) / 1000, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        **(extra or {}),
    }


def start() -> EventState:
    state = EventState()
    state.accept(event("assistant.turn_start", {"turnId": "0", "interactionId": "run"}))
    return state


def launch(state: EventState, id_: str = "shell-a", detach: bool = False, async_: bool = False) -> None:
    call = event(
        "tool.execution_start",
        {
            "toolName": "powershell",
            "toolCallId": f"launch-{id_}",
            "arguments": {"detach": detach, "command": "PRIVATE COMMAND SENTINEL", "mode": "async" if async_ else "sync"},
        },
    )
    state.accept(call)
    if async_:
        content = f"<command started in {'detached ' if detach else ''}background with shellId: {id_}>"
    else:
        content = (
            f"<command with shellId: {id_} is still running after 180 seconds. The command is still running "
            "but hasn't produced output yet. You will be automatically notified when it completes; if you need "
            "the command to complete end your response with no tool calls to wait for the notification, or use "
            "stop_powershell to stop it.>"
        )
    state.accept(
        event(
            "tool.execution_complete",
            {"toolCallId": f"launch-{id_}", "success": True, "result": {"content": content}},
        )
    )


def final(state: EventState) -> None:
    state.accept(event("assistant.message", {"turnId": "0", "toolRequests": [], "content": "PRIVATE RESPONSE SENTINEL"}))
    state.accept(event("assistant.turn_end", {"turnId": "0"}))


def sample(id_: str, state: EventState, busy: bool = False, parent_id: str | None = None, extra: dict | None = None) -> dict[str, Any]:
    return {
        "id": id_,
        "parentId": parent_id,
        "title": id_,
        "source": "Copilot desktop",
        "busy": busy,
        "alive": True,
        "owner": "live-owner",
        "contextOnly": False,
        "events": state.snapshot(),
        **(extra or {}),
    }


def rig(retained=(), dismissed=None):
    alerts: list[dict[str, Any]] = []

    async def _emit(key: str, value: dict[str, Any]) -> None:
        alerts.append({"key": key, **value})

    return alerts, FamilyMonitor("LOCAL", _emit, retained, dismissed)


async def test_foreground_idle_plus_final_response_does_not_finish_attached_shell_work():
    alerts, monitor = rig()
    parent, child = start(), start()
    await monitor.update([sample("parent", parent, True), sample("child", child, True, "parent")], {"now": 1000})
    launch(child)
    final(parent)
    final(child)
    view = await monitor.update([sample("parent", parent), sample("child", child, False, "parent")], {"now": 2000})
    assert child.snapshot()["activeTurn"] is False
    assert child.snapshot()["backgroundCount"] == 1
    assert child.snapshot()["terminal"] is None
    assert view["sessions"][0]["state"] == "working"
    assert view["sessions"][0]["runningCount"] == 1
    assert view["sessions"][0]["finishedAt"] is None
    assert view["sessions"][0]["dismissKey"] is None
    assert len(alerts) == 0
    child.accept(event("system.notification", {"kind": {"type": "shell_completed", "shellId": "shell-a", "exitCode": 0}}))
    view = await monitor.update([sample("parent", parent), sample("child", child, False, "parent")], {"now": 3000})
    assert view["sessions"][0]["state"] == "finished"
    assert len([a for a in alerts if a["kind"] == "finished"]) == 1
    await monitor.update([sample("parent", parent), sample("child", child, False, "parent")], {"now": 4000})
    assert len(alerts) == 1
    import json

    assert "PRIVATE" not in json.dumps(child.snapshot())


async def test_restart_reconciles_misclassified_dismissed_finished_descendants_without_replay():
    parent, child, grandchild = start(), start(), start()
    original_alerts, original_monitor = rig()

    def rows():
        return [
            sample("parent", parent),
            sample("child", child, False, "parent"),
            sample("grandchild", grandchild, False, "child"),
        ]

    await original_monitor.update([{**row, "busy": True} for row in rows()], {"now": 1000})
    final(parent)
    final(child)
    final(grandchild)
    completed = await original_monitor.update(rows(), {"now": 2000})
    original_monitor.dismiss([{"id": "parent", "key": completed["sessions"][0]["dismissKey"]}])
    launch(grandchild)
    restarted_alerts, restarted_monitor = rig(completed["members"], original_monitor.dismissed)
    active = await restarted_monitor.update(rows(), {"now": 3000})
    assert len(active["sessions"]) == 1
    assert active["sessions"][0]["state"] == "working"
    assert active["sessions"][0]["runningCount"] == 1
    assert next(r for r in active["members"] if r["id"] == "grandchild")["finishedAt"] is None
    assert len(restarted_monitor.dismissed) == 0
    assert len(restarted_alerts) == 0
    result = restarted_monitor.dismiss([{"id": "parent", "key": completed["sessions"][0]["dismissKey"]}])
    assert len(result["skipped"]) == 1


def test_detached_servers_and_explicitly_stopped_commands_do_not_create_false_background_work():
    for detached in (False, True):
        state = start()
        launch(state, detach=detached, async_=True)
        if not detached:
            state.accept(event("tool.execution_start", {"toolName": "stop_powershell", "toolCallId": "stop", "arguments": {"shellId": "shell-a"}}))
            state.accept(
                event(
                    "tool.execution_complete",
                    {"toolCallId": "stop", "success": True, "result": {"content": "<command with id: shell-a stopped>"}},
                )
            )
            state.accept(event("assistant.turn_start", {"turnId": "0", "interactionId": "run"}))
        final(state)
        assert state.snapshot()["backgroundCount"] == 0
        assert state.snapshot()["backgroundUnconfirmed"] is False
        assert state.snapshot()["terminal"]


def test_sync_command_auto_moved_to_background_tracked_by_real_shell_id_not_unconfirmed():
    _, monitor = rig()
    state = start()
    state.accept(event("tool.execution_start", {"toolName": "powershell", "toolCallId": "moved", "arguments": {}}))
    state.accept(
        event(
            "tool.execution_complete",
            {
                "toolCallId": "moved",
                "success": True,
                "result": {
                    "content": (
                        "<command with shellId: 93 moved to background by the user. You will be automatically "
                        "notified when it completes. The user has already seen a UI confirmation — do NOT respond "
                        "to them about this and do NOT call any more tools for this command. Wait for the user's "
                        "next instruction.>"
                    )
                },
            },
        )
    )
    final(state)
    assert state.snapshot()["backgroundCount"] == 1
    assert state.snapshot()["backgroundUnconfirmed"] is False
    assert state.snapshot()["terminal"] is None
    state.accept(event("system.notification", {"kind": {"type": "shell_completed", "shellId": "93", "exitCode": 0}}))
    assert state.snapshot()["backgroundCount"] == 0
    assert state.snapshot()["terminal"]


def test_read_return_is_not_process_exit_explicit_native_exit_metadata_settles_attached_command():
    state = start()
    launch(state)
    final(state)
    state.accept(event("tool.execution_start", {"toolName": "read_powershell", "toolCallId": "read", "arguments": {"shellId": "shell-a"}}))
    state.accept(
        event(
            "tool.execution_complete",
            {
                "toolCallId": "read",
                "success": True,
                "result": {"content": "PRIVATE OUTPUT\n<command with shellId: shell-a is still running after 120 seconds. No output yet.>"},
            },
        )
    )
    assert state.snapshot()["backgroundCount"] == 1
    assert state.snapshot()["terminal"] is None
    state.accept(event("tool.execution_start", {"toolName": "read_powershell", "toolCallId": "read-2", "arguments": {"shellId": "shell-a"}}))
    state.accept(
        event(
            "tool.execution_complete",
            {
                "toolCallId": "read-2",
                "success": True,
                "result": {"contents": [{"type": "shell_exit", "shellId": "shell-a", "exitCode": 0, "outputPreview": "PRIVATE"}]},
            },
        )
    )
    assert state.snapshot()["backgroundCount"] == 0
    assert state.snapshot()["terminal"]


def test_only_native_shell_status_envelopes_count_command_text_and_detached_output_do_not():
    state = start()
    state.accept(event("tool.execution_start", {"toolName": "powershell", "toolCallId": "sync", "arguments": {}}))
    state.accept(
        event(
            "tool.execution_complete",
            {
                "toolCallId": "sync",
                "success": True,
                "result": {"content": "still running shellId: 999\n<shellId: 1 completed with exit code 0>"},
            },
        )
    )
    final(state)
    assert state.snapshot()["backgroundCount"] == 0
    assert state.snapshot()["backgroundUnconfirmed"] is False
    assert state.snapshot()["terminal"]

    detached = start()
    launch(detached, async_=True, detach=True)
    final(detached)
    detached.accept(event("tool.execution_start", {"toolName": "read_powershell", "toolCallId": "read", "arguments": {"shellId": "shell-a"}}))
    detached.accept(
        event(
            "tool.execution_complete",
            {
                "toolCallId": "read",
                "success": True,
                "result": {"content": "PRIVATE\n<command with shellId: shell-a is still running after 10 seconds. No output yet.>"},
            },
        )
    )
    assert detached.snapshot()["backgroundCount"] == 0
    assert detached.snapshot()["terminal"]


async def test_unknown_failed_or_cancelled_background_outcomes_never_imply_successful_completion():
    for mode in ("unknown", "failed", "cancelled", "owner-changed", "dead"):
        alerts, monitor = rig()
        state = start()
        launch(state)
        final(state)
        await monitor.update([sample("child", state)], {"now": 1000})
        if mode in ("unknown", "failed"):
            state.accept(
                event(
                    "system.notification",
                    {"kind": {"type": "shell_completed", "shellId": "shell-a", **({"exitCode": 1} if mode == "failed" else {})}},
                )
            )
        if mode == "cancelled":
            state.accept(event("tool.execution_start", {"toolName": "stop_powershell", "toolCallId": "stop", "arguments": {"shellId": "shell-a"}}))
            state.accept(
                event(
                    "tool.execution_complete",
                    {"toolCallId": "stop", "success": True, "result": {"content": "<command with id: shell-a stopped>"}},
                )
            )
        extra = (
            {"owner": "new-owner", "activityUnconfirmed": "Owner changed"}
            if mode == "owner-changed"
            else {"alive": False}
            if mode == "dead"
            else {}
        )
        view = await monitor.update([sample("child", state, False, None, extra)], {"now": 2000})
        assert view["sessions"][0]["state"] != "finished", mode
        assert view["sessions"][0]["finishedAt"] is None, mode
        if view["sessions"][0]["state"] == "unknown":
            assert re.match(r"^[a-f0-9]{64}$", view["sessions"][0]["dismissKey"]), mode
        else:
            assert view["sessions"][0]["dismissKey"] is None, mode
        assert not any(a["kind"] == "finished" for a in alerts), mode


def test_nested_task_agents_hold_root_run_open_without_replacing_response_or_identity():
    state = start()
    run_id = state.run_id
    state.accept(event("subagent.started", {"executionMode": "background"}, {"agentId": "agent"}))
    state.accept(event("subagent.started", {"executionMode": "background", "parentId": "agent"}, {"agentId": "nested"}))
    final(state)
    response = state.last_response_at
    state.accept(event("assistant.message", {"content": "PRIVATE AGENT"}, {"agentId": "nested"}))
    state.accept(event("subagent.completed", {}, {"agentId": "agent"}))
    assert state.snapshot()["backgroundCount"] == 1
    assert state.snapshot()["terminal"] is None
    state.accept(event("subagent.completed", {}, {"agentId": "nested"}))
    assert state.snapshot()["terminal"]
    assert state.last_response_at == response
    assert state.run_id == run_id


def test_task_complete_markers_cannot_override_pending_work_or_background_agent_failure_cancellation():
    for type_ in ("subagent.failed", "subagent.completed", "system.notification"):
        state = start()
        state.accept(event("subagent.started", {"executionMode": "background"}, {"agentId": "agent"}))
        state.accept(event("session.task_complete", {"success": True}))
        assert state.snapshot()["terminal"] is None
        final(state)
        state.accept(
            event(type_, {"kind": {"type": "agent_completed", "agentId": "agent", "status": "failed"}})
            if type_ == "system.notification"
            else event(type_, {"cancelled": type_ == "subagent.completed"}, {"agentId": "agent"})
        )
        assert state.snapshot()["backgroundCount"] == 0
        assert state.snapshot()["terminal"] is None
        assert state.snapshot()["error"]


def test_bare_exit_code_with_no_shell_id_never_leaves_a_stuck_unknown_entry():
    # A synchronous `powershell` call that finishes within its own tool-call turnaround
    # (never backgrounded) is reported with a bare exit-code envelope and no shellId at
    # all. Previously this fell through to the "unresolvable" branch and permanently
    # pinned backgroundUnconfirmed to True under a synthetic call-id key that nothing
    # could ever clear, leaving the family forever stuck in "unknown".
    state = start()
    state.accept(event("tool.execution_start", {"toolName": "powershell", "toolCallId": "sync-fast", "arguments": {}}))
    state.accept(
        event(
            "tool.execution_complete",
            {"toolCallId": "sync-fast", "success": True, "result": {"content": "<exited with exit code 0>"}},
        )
    )
    final(state)
    assert state.snapshot()["backgroundCount"] == 0
    assert state.snapshot()["backgroundUnconfirmed"] is False
    assert state.snapshot()["terminal"]


def test_session_id_labeled_background_commands_are_tracked_like_shell_id_ones():
    # Commands launched with an explicit sessionId argument (e.g. a persistent LSP
    # server) are reported using a `sessionId:` label instead of `shellId:`. Previously
    # unrecognized, so they fell through to "unknown" and never resolved even though a
    # later matching completion event for the same id arrived.
    state = start()
    state.accept(
        event(
            "tool.execution_start",
            {"toolName": "powershell", "toolCallId": "lsp-launch", "arguments": {"sessionId": "lsp", "mode": "async"}},
        )
    )
    state.accept(
        event(
            "tool.execution_complete",
            {
                "toolCallId": "lsp-launch",
                "success": True,
                "result": {"content": "<command started in background with sessionId: lsp>"},
            },
        )
    )
    final(state)
    assert state.snapshot()["backgroundCount"] == 1
    assert state.snapshot()["backgroundUnconfirmed"] is False
    assert state.snapshot()["terminal"] is None

    state.accept(event("system.notification", {"kind": {"type": "shell_completed", "shellId": "lsp", "exitCode": 0}}))
    assert state.snapshot()["backgroundCount"] == 0
    assert state.snapshot()["terminal"]


async def test_family_recovers_from_unknown_once_startup_gap_background_bugs_are_fixed():
    # End-to-end regression for the reported symptom: a session whose only background
    # work is an unrecognized-at-the-time completion envelope must not stay permanently
    # "unknown" at the family level once a healthy, confirmed poll follows.
    alerts, monitor = rig()
    state = start()
    await monitor.update([sample("child", state, True)], {"now": 1000})
    state.accept(event("tool.execution_start", {"toolName": "powershell", "toolCallId": "sync-fast", "arguments": {}}))
    state.accept(
        event(
            "tool.execution_complete",
            {"toolCallId": "sync-fast", "success": True, "result": {"content": "<exited with exit code 0>"}},
        )
    )
    final(state)
    view = await monitor.update([sample("child", state)], {"now": 2000})
    assert view["sessions"][0]["state"] == "finished"
    view = await monitor.update([sample("child", state)], {"now": 2000})
    assert view["sessions"][0]["state"] == "finished"
    assert not any(a["kind"] == "warning" for a in alerts)


async def test_unsupported_shell_status_is_unconfirmed_but_cannot_hide_separately_proven_running_work():
    _, monitor = rig()
    state = start()
    state.accept(event("tool.execution_start", {"toolName": "powershell", "toolCallId": "unknown", "arguments": {}}))
    state.accept(
        event(
            "tool.execution_complete",
            {"toolCallId": "unknown", "success": True, "result": {"content": "unsupported"}},
        )
    )
    result = await monitor.update([sample("child", state, True)], {"now": 1000})
    assert result["sessions"][0]["state"] == "working"
    final(state)
    result = await monitor.update([sample("child", state)], {"now": 2000})
    assert result["sessions"][0]["state"] == "unknown"
