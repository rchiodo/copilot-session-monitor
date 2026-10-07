"""Port of test/families.test.mjs -- family grouping, ordering, alert
provenance, dismissal-safe persistence, and app-session hierarchy linkage.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

import pytest

from pymonitor.engine import Ledger, SessionStore
from pymonitor.events import EventState
from pymonitor.families import FamilyMonitor, group_families
from pymonitor.hierarchy import hierarchy_index, related_metadata, root_of, selected_hierarchy

_serial = 0


def event(state: EventState, type_: str, data: dict | None = None) -> None:
    global _serial
    _serial += 1
    state.accept(
        {
            "id": f"e{_serial}",
            "timestamp": datetime.fromtimestamp((1790000000000 + _serial * 1000) / 1000, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "type": type_,
            "data": data or {},
        }
    )


def run() -> EventState:
    state = EventState()
    event(state, "assistant.turn_start", {"interactionId": f"run{_serial}", "turnId": "0"})
    return state


def finish(state: EventState) -> None:
    event(state, "assistant.message", {"phase": "final_answer", "turnId": "0", "content": "PRIVATE TRANSCRIPT", "toolRequests": []})
    event(state, "assistant.turn_end", {"turnId": "0"})


def sample(id_: str, state: EventState, parent_id: str | None = None, extra: dict | None = None) -> dict[str, Any]:
    return {
        "id": id_,
        "title": f"Title {id_}",
        "source": "Copilot desktop",
        "busy": True,
        "alive": True,
        "owner": "pid:start",
        "interrupted": False,
        "events": state.snapshot(),
        "parentId": parent_id,
        "hierarchyIssue": None,
        **(extra or {}),
    }


def rig(retained=()):
    alerts: list[dict[str, Any]] = []
    keys: set[str] = set()

    async def _emit(key: str, alert: dict[str, Any]) -> None:
        if key not in keys:
            keys.add(key)
            alerts.append({"key": key, **alert})

    monitor = FamilyMonitor("TEST", _emit, retained)
    now = [1000]

    async def step(samples, options: dict[str, Any] | None = None):
        now[0] += 1000
        merged = {"now": now[0], **(options or {})}
        return await monitor.update(samples, merged)

    return monitor, alerts, step


async def test_parent_finishes_while_child_works_one_card_no_early_notification():
    monitor, alerts, step = rig()
    parent, child = run(), run()
    await step([sample("parent", parent), sample("child", child, "parent")])
    finish(parent)
    view = await step([sample("parent", parent, None, {"busy": False}), sample("child", child, "parent")])
    assert len(view["sessions"]) == 1
    assert view["sessions"][0]["id"] == "parent"
    assert view["sessions"][0]["state"] == "working"
    assert view["sessions"][0]["runningCount"] == 1
    assert view["sessions"][0]["parentAlert"]["kind"] == "finished"
    assert view["sessions"][0]["parentAlert"]["sessionId"] == "parent"
    assert len(alerts) == 0
    parent_alert = view["sessions"][0]["parentAlert"]
    finish(child)
    view = await step([sample("parent", parent, None, {"busy": False}), sample("child", child, "parent", {"busy": False})])
    assert view["sessions"][0]["state"] == "finished"
    assert view["sessions"][0]["parentAlert"] == parent_alert
    assert monitor.rows["child"]["lastAlert"]["sessionId"] == "child"
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "finished"
    assert alerts[0]["title"] == "Title parent"
    await step([sample("parent", parent, None, {"busy": False}), sample("child", child, "parent", {"busy": False})])
    assert len(alerts) == 1
    assert "PRIVATE TRANSCRIPT" not in json.dumps(view)


async def test_idle_ancestor_child_only_run_nested_grandchild_aggregate_without_reconstructing_parent_alert():
    monitor, alerts, step = rig()
    parent, child, grandchild = run(), run(), run()
    finish(parent)

    def rows():
        return [
            sample("p", parent, None, {"busy": False, "contextOnly": True}),
            sample("c", child, "p", {"busy": not child.terminal}),
            sample("g", grandchild, "c", {"busy": not grandchild.terminal}),
        ]

    view = await step(rows())
    assert len(view["sessions"]) == 1
    assert view["sessions"][0]["parentState"] == "idle"
    assert view["sessions"][0]["parentAlert"] is None
    assert view["sessions"][0]["childCount"] == 2
    finish(child)
    view = await step(rows())
    assert view["sessions"][0]["state"] == "working"
    assert len(alerts) == 0
    finish(grandchild)
    view = await step(rows())
    assert view["sessions"][0]["state"] == "finished"
    assert view["sessions"][0]["parentAlert"] is None
    assert len(alerts) == 1


async def test_families_order_and_last_response_reflect_most_recent_member_not_just_parent():
    monitor, alerts, step = rig()
    p, c, other = run(), run(), run()
    event(p, "assistant.message", {"phase": "commentary"})
    event(other, "assistant.message", {"phase": "commentary"})
    event(c, "assistant.message", {"phase": "commentary"})
    view = await step([sample("p", p), sample("c", c, "p"), sample("independent", other)])
    # c responded last, so the family card (and its sort position) must reflect
    # c's time, not just parent p's own (earlier) last response.
    assert [row["id"] for row in view["sessions"]] == ["p", "independent"]
    assert view["sessions"][0]["lastResponseAt"] == c.last_response_at
    assert len(group_families(view["members"])) == 2
    fallback = [{**row, "lastResponseAt": None} if row["id"] in ("p", "c") else row for row in view["members"]]
    nulled = group_families(fallback)
    assert nulled[1]["lastResponseAt"] is None
    assert nulled[1]["firstObservedAt"] == view["sessions"][0]["firstObservedAt"]


async def test_parent_waiting_and_error_alerts_update_independently_while_child_works():
    monitor, alerts, step = rig()
    p, c = run(), run()
    await step([sample("p", p), sample("c", c, "p")])
    event(p, "user_input.requested", {"requestId": "parent-question"})
    waiting = await step([sample("p", p), sample("c", c, "p")])
    assert waiting["sessions"][0]["state"] == "working"
    assert waiting["sessions"][0]["parentAlert"]["kind"] == "waiting"
    event(p, "assistant.turn_start", {"interactionId": "parent-resumed", "turnId": "0"})
    await step([sample("p", p), sample("c", c, "p")])
    event(p, "session.error")
    failed = await step([sample("p", p, None, {"busy": False}), sample("c", c, "p")])
    assert failed["sessions"][0]["state"] == "working"
    assert failed["sessions"][0]["parentAlert"]["kind"] == "error"
    assert failed["sessions"][0]["parentAlert"]["sessionId"] == "p"
    assert failed["sessions"][0]["parentAlert"]["at"] != waiting["sessions"][0]["parentAlert"]["at"]


async def test_late_parent_response_metadata_updates_ordering_without_replacing_completion_or_alert():
    monitor, alerts, step = rig()
    p, c = run(), run()
    await step([sample("p", p), sample("c", c, "p")])
    finish(p)
    first = await step([sample("p", p, None, {"busy": False}), sample("c", c, "p")])
    event(p, "assistant.message", {"phase": "final_answer"})
    later = await step([sample("p", p, None, {"busy": False}), sample("c", c, "p")])
    assert later["sessions"][0]["lastResponseAt"] == p.last_response_at
    assert later["sessions"][0]["parentAlert"] == first["sessions"][0]["parentAlert"]
    assert next(r for r in later["members"] if r["id"] == "p")["finishedAt"] == next(
        r for r in first["members"] if r["id"] == "p"
    )["finishedAt"]
    event(p, "session.error")
    finish(c)
    failed = await step([sample("p", p, None, {"busy": False}), sample("c", c, "p", {"busy": False})])
    assert failed["sessions"][0]["state"] == "error"
    assert failed["sessions"][0]["parentAlert"]["kind"] == "error"
    assert not any(a["kind"] == "finished" for a in alerts)


@pytest.mark.parametrize("kind,expected", [("user_input.requested", "waiting"), ("session.error", "error"), ("abort", "error")])
async def test_descendant_event_is_not_completion_and_does_not_overwrite_parent_alert(kind, expected):
    monitor, alerts, step = rig()
    p, c = run(), run()
    await step([sample("p", p), sample("c", c, "p")])
    finish(p)
    await step([sample("p", p, None, {"busy": False}), sample("c", c, "p")])
    event(c, kind, {"requestId": "question"})
    view = await step([sample("p", p, None, {"busy": False}), sample("c", c, "p", {"busy": False})])
    assert view["sessions"][0]["state"] == expected
    assert view["sessions"][0]["parentAlert"]["kind"] == "finished"
    assert len(alerts) == 1
    assert alerts[0]["kind"] == expected
    assert not any(a["kind"] == "finished" for a in alerts)


async def test_simultaneous_child_completions_yield_one_family_notification_new_runs_rearm_once():
    monitor, alerts, step = rig()
    p, a, b = run(), run(), run()

    def rows():
        return [
            sample("p", p, None, {"busy": not p.terminal}),
            sample("a", a, "p", {"busy": not a.terminal}),
            sample("b", b, "p", {"busy": not b.terminal}),
        ]

    await step(rows())
    for s in (p, a, b):
        finish(s)
    await step(rows())
    assert len(alerts) == 1
    event(a, "assistant.turn_start", {"interactionId": "next", "turnId": "0"})
    view = await step(rows())
    assert view["sessions"][0]["state"] == "working"
    finish(a)
    await step(rows())
    assert len(alerts) == 2
    assert alerts[0]["key"] != alerts[1]["key"]


async def test_missing_dead_disconnected_rotated_relatives_and_gaps_never_finish_a_family():
    for problem in ("missing", "dead", "disconnect", "rotation", "gap"):
        monitor, alerts, step = rig()
        p, c = run(), run()
        await step([sample("p", p), sample("c", c, "p")])
        finish(p)
        finish(c)
        rows = [sample("p", p, None, {"busy": False}), sample("c", c, "p", {"busy": False})]
        if problem == "missing":
            rows.pop()
        if problem == "dead":
            rows[1]["alive"] = False
        if problem == "rotation":
            rows[1]["events"]["replaced"] = True
        options = {"healthy": False} if problem == "disconnect" else {"now": 90000} if problem == "gap" else {}
        view = await step(rows, options)
        assert view["sessions"][0]["state"] == "unknown", problem
        assert not any(a["kind"] == "finished" for a in alerts), problem


async def test_delayed_terminal_flush_remains_eligible_unlike_lost_completion_authority():
    monitor, alerts, step = rig()
    p = run()
    await step([sample("p", p)])
    view = await step([sample("p", p, None, {"busy": False})])
    assert view["sessions"][0]["state"] == "unknown"
    finish(p)
    await step([sample("p", p, None, {"busy": False})])
    assert alerts[0]["kind"] == "finished"


async def test_detaching_a_working_child_never_manufactures_completion_for_the_old_family():
    monitor, alerts, step = rig()
    p, c = run(), run()
    await step([sample("p", p), sample("c", c, "p")])
    finish(p)
    await step([sample("p", p, None, {"busy": False}), sample("c", c, "p")])
    detached = await step([sample("p", p, None, {"busy": False}), sample("c", c)])
    assert len(detached["sessions"]) == 2
    assert len(alerts) == 0
    finish(c)
    await step([sample("p", p, None, {"busy": False}), sample("c", c, None, {"busy": False})])
    assert len(alerts) == 1
    assert alerts[0]["title"] == "Title c"


async def test_missing_parent_or_cyclic_hierarchy_is_visible_and_cannot_generate_completion():
    for cycle in (False, True):
        monitor, alerts, step = rig()
        a, b = run(), run()

        def rows():
            result = [sample("a", a, "b" if cycle else "missing", {"busy": not a.terminal})]
            if cycle:
                result.append(sample("b", b, "a", {"busy": not b.terminal}))
            return result

        working = await step(rows())
        assert len(working["sessions"]) == 1
        pattern = "Cycle" if cycle else "missing"
        assert re.search(pattern, working["sessions"][0]["hierarchyIssue"])
        finish(a)
        finish(b)
        view = await step(rows())
        assert view["sessions"][0]["state"] == "unknown"
        assert len(alerts) == 0


async def test_family_persistence_retains_relationships_and_exact_parent_alert_restart_has_no_replay(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))
    ledger = Ledger(str(tmp_path / "ledger.json"))
    await ledger.load()
    delivered: list[dict[str, Any]] = []

    def create(retained=()):
        async def _emit(key: str, alert: dict[str, Any]) -> None:
            if await ledger.claim(key):
                delivered.append(alert)

        return FamilyMonitor("TEST", _emit, retained)

    monitor = create()
    p, c = run(), run()
    await monitor.update([sample("p", p), sample("c", c, "p")], {"now": 1000})
    finish(p)
    running = await monitor.update([sample("p", p, None, {"busy": False}), sample("c", c, "p")], {"now": 2000})
    await store.save(running["members"])
    restored = create(await SessionStore(store.file).load())
    assert restored.snapshot()["sessions"][0]["state"] == "unknown"
    assert restored.snapshot()["sessions"][0]["parentAlert"] == running["sessions"][0]["parentAlert"]
    finish(c)
    await restored.update([sample("p", p, None, {"busy": False}), sample("c", c, "p", {"busy": False})], {"now": 3000})
    assert len(delivered) == 0
    event(c, "assistant.turn_start", {"interactionId": "fresh", "turnId": "0"})
    await restored.update([sample("p", p, None, {"busy": False}), sample("c", c, "p")], {"now": 4000})
    finish(c)
    done = await restored.update([sample("p", p, None, {"busy": False}), sample("c", c, "p", {"busy": False})], {"now": 5000})
    assert len(delivered) == 1
    await store.save(done["members"])
    again = create(await SessionStore(store.file).load())
    await again.update([sample("p", p, None, {"busy": False}), sample("c", c, "p", {"busy": False})], {"now": 6000})
    assert len(delivered) == 1
    assert again.snapshot()["sessions"][0]["state"] == "finished"
    from pathlib import Path

    assert "PRIVATE TRANSCRIPT" not in Path(store.file).read_text(encoding="utf-8")


async def test_finished_family_restored_from_before_this_process_started_is_hidden_until_genuinely_new_work(tmp_path):
    store = SessionStore(str(tmp_path / "sessions.json"))

    def create(retained=(), process_started_at_ms=None):
        async def _emit(key: str, alert: dict[str, Any]) -> None:
            pass

        return FamilyMonitor("TEST", _emit, retained, process_started_at_ms=process_started_at_ms)

    monitor = create()
    p = run()
    await monitor.update([sample("p", p)], {"now": 1000})
    finish(p)
    done = await monitor.update([sample("p", p, None, {"busy": False})], {"now": 2000})
    assert done["sessions"][0]["state"] == "finished"
    await store.save(done["members"])

    # Bug #1 repro: with no process_started_at_ms (the pre-fix behavior, and
    # still the default for every other FamilyMonitor caller/test), a
    # restored "finished" row stays visible forever, regardless of how long
    # ago it actually finished relative to the current process's lifetime.
    restored_default = create(await SessionStore(store.file).load())
    assert restored_default.snapshot()["sessions"][0]["state"] == "finished"

    # Fix: a new process that started well after the row's finishedAt
    # (simulated here via a "now" far in the future of the saved finishedAt)
    # hides that stale row entirely instead of showing it as freshly
    # finished.
    restored = create(await SessionStore(store.file).load(), process_started_at_ms=50_000)
    assert restored.snapshot()["sessions"] == []

    # The hidden row re-surfaces once the same family produces genuinely new
    # completion work (a different dismissKey) after this process started --
    # the exact same dynamic dismissed-key check "Clear retained" relies on.
    event(p, "assistant.turn_start", {"interactionId": "fresh", "turnId": "0"})
    await restored.update([sample("p", p)], {"now": 51000})
    finish(p)
    done2 = await restored.update([sample("p", p, None, {"busy": False})], {"now": 52000})
    assert done2["sessions"][0]["state"] == "finished"


def _schema() -> dict[str, Any]:
    return {
        "sessions": [
            {
                "id": id_,
                "title": id_,
                "session_type": "general_chat" if id_ == "chat" else "project",
                "execution_location": "local",
                "is_running": 1 if id_ == "nested" else 0,
            }
            for id_ in ("chat", "old-parent", "parent", "child", "nested", "side", "unrelated")
        ],
        "workspaces": [
            {"id": "wp", "session_id": "parent", "creator_session_id": "chat", "host_id": "local"},
            {"id": "wc", "session_id": "child", "creator_session_id": "old-parent", "host_id": "local"},
            {"id": "wn", "session_id": "nested", "creator_session_id": "child", "host_id": "local"},
        ],
        "links": [
            {"child_workspace_id": "wc", "parent_workspace_id": "wp"},
            {"child_workspace_id": "wn", "parent_workspace_id": "wc"},
        ],
        "aliases": [{"session_id": "old-parent", "workspace_id": "wp"}],
        "workspaceChats": [],
        "sessionChats": [],
    }


def _index(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return hierarchy_index(
        sessions=data["sessions"],
        workspaces=data["workspaces"],
        links=data["links"],
        aliases=data["aliases"],
        workspace_chats=data["workspaceChats"],
        session_chats=data["sessionChats"],
    )


def test_canonical_workspace_ids_runtime_aliases_chat_creators_nested_ancestry_resolve_without_history_import():
    nodes = _index(_schema())
    assert nodes["child"]["parentId"] == "parent"
    assert nodes["old-parent"]["parentId"] == "parent"
    assert nodes["parent"]["parentId"] == "chat"
    assert root_of("nested", nodes)["id"] == "chat"
    assert sorted(row["id"] for row in selected_hierarchy(nodes, set())) == ["chat", "child", "nested", "parent"]
    assert not any(row["id"] == "unrelated" for row in selected_hierarchy(nodes, set()))
    assert not any(row["id"] == "old-parent" for row in related_metadata(nodes, selected_hierarchy(nodes, set())))


def test_recorded_side_chats_missing_links_cycles_detach_remote_relatives_are_conservative():
    data = _schema()
    data["workspaceChats"].append({"workspace_id": "wc", "session_id": "side"})
    nodes = _index(data)
    assert nodes["side"]["parentId"] == "child"
    data["sessionChats"].append({"parent_session_id": "chat", "session_id": "unrelated"})
    assert _index(data)["unrelated"]["parentId"] == "chat"
    data["links"][0]["parent_workspace_id"] = "gone"
    nodes = _index(data)
    assert re.search("missing", nodes["child"]["hierarchyIssue"])
    assert re.search("Unavailable parent", nodes["gone"]["title"])
    data["links"][0]["parent_workspace_id"] = "wn"
    assert re.search("Cycle", root_of("nested", _index(data))["issue"])
    data["links"] = []
    data["workspaces"][1]["creator_session_id"] = None
    assert _index(data)["child"]["parentId"] is None
    data["workspaces"][2]["host_id"] = "remote-host"
    nodes = _index(data)
    assert next(row for row in selected_hierarchy(nodes, {"child"}) if row["id"] == "nested")["local"] is False


def test_ambiguous_identity_aliases_and_conflicting_recorded_parents_are_surfaced():
    data = _schema()
    data["aliases"].append({"session_id": "old-parent", "workspace_id": "wc"})
    data["sessionChats"].append({"session_id": "side", "parent_session_id": "old-parent"})
    assert re.search("Ambiguous", _index(data)["side"]["hierarchyIssue"])
    data["workspaceChats"].append({"session_id": "side", "workspace_id": "wn"})
    assert re.search("Conflicting", _index(data)["side"]["hierarchyIssue"])
