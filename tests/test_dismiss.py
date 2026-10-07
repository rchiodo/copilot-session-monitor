"""Port of test/dismiss.test.mjs -- MonitorActions serialization, dismiss
validation/replay-safety, and readDismissEntries HTTP body parsing.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import pytest

from pymonitor.actions import DismissError, MonitorActions, read_dismiss_entries
from pymonitor.engine import Ledger, SessionStore
from pymonitor.events import EventState
from pymonitor.families import FamilyMonitor, group_families

AT = "2026-10-02T20:00:00.000Z"


def row(id_: str, state: str = "finished", parent_id: str | None = None) -> dict[str, Any]:
    return {
        "id": id_,
        "title": f"Title {id_}",
        "machine": "TEST",
        "source": "Copilot desktop",
        "state": state,
        "parentId": parent_id,
        "detail": "Fixture status",
        "activity": "Agent running",
        "runId": f"old-{id_}",
        "firstObservedAt": AT,
        "startedAt": AT,
        "lastEventAt": AT,
        "lastResponseAt": AT,
        "finishedAt": AT if state == "finished" else None,
        "contextOnly": False,
        "hierarchyIssue": None,
        "lastAlert": None,
    }


def entries(monitor: FamilyMonitor) -> list[dict[str, Any]]:
    return [
        {"id": item["id"], "key": item.get("dismissKey") or "a" * 64}
        for item in group_families(list(monitor.rows.values()))
    ]


def sample(id_: str, events: EventState, parent_id: str | None = None, busy: bool = True, **extra: Any) -> dict[str, Any]:
    return {
        "id": id_,
        "title": f"Title {id_}",
        "source": "Copilot desktop",
        "events": events.snapshot(),
        "alive": True,
        "owner": "owner",
        "parentId": parent_id,
        "busy": busy,
        **extra,
    }


def event(events: EventState, type_: str, id_: str, data: dict | None = None) -> None:
    events.accept({"type": type_, "id": id_, "timestamp": AT, "data": data or {}})


def test_individual_bulk_dismissal_hides_finished_and_unconfirmed_preserves_active():
    rows = [
        row("p"), row("c", "finished", "p"), row("another"),
        row("work", "working"), row("wait", "waiting"), row("err", "error"), row("unknown", "unknown"),
    ]

    async def _emit(*_a, **_k) -> None:
        return None

    monitor = FamilyMonitor("TEST", _emit)
    monitor.engine.rows = {item["id"]: item for item in rows}
    before = list(monitor.rows.items())
    request = entries(monitor)
    assert monitor.dismiss(request[:1])["dismissed"] == [request[0]["id"]]
    result = monitor.dismiss(request)
    assert len(result["dismissed"]) == 3
    assert len(result["skipped"]) == 3
    assert len(monitor.snapshot()["sessions"]) == 3
    assert list(monitor.rows.items()) == before


@pytest.mark.asyncio
async def test_unconfirmed_family_can_be_dismissed_survives_restart_restored_by_new_work(tmp_path: Path):
    async def _emit(*_a, **_k) -> None:
        return None

    store = SessionStore(str(tmp_path / "sessions.json"))
    monitor = FamilyMonitor("TEST", _emit, [row("stuck", "unknown")])
    (entry,) = entries(monitor)
    assert re.fullmatch(r"[a-f0-9]{64}", entry["key"])
    assert monitor.dismiss([entry])["dismissed"] == ["stuck"]
    assert len(monitor.snapshot()["sessions"]) == 0
    await store.save(monitor.snapshot()["members"], monitor.dismissed)
    loaded = SessionStore(store.file)

    async def _fail(*_a, **_k) -> None:
        pytest.fail("No historical alert")

    restored = FamilyMonitor("TEST", _fail, await loaded.load(), loaded.dismissed)
    assert len(restored.snapshot()["sessions"]) == 0
    events = EventState()
    event(events, "assistant.turn_start", "start", {"interactionId": "new-stuck", "turnId": "0"})
    working = await restored.update([sample("stuck", events)], {"now": 1000})
    assert working["sessions"][0]["state"] == "working"
    assert len(restored.dismissed) == 0


def test_dismiss_key_value_is_no_longer_compared_only_current_eligibility_gates_it():
    async def _emit(*_a, **_k) -> None:
        return None

    monitor = FamilyMonitor("TEST", _emit, [row("stuck", "unknown")])
    # Any syntactically valid key dismisses an eligible row now: the exact-match
    # requirement was the source of "Clear retained sometimes does nothing" (any
    # poll tick between the dashboard's last render and the click changed the
    # hash). Eligibility (dismissKey truthy, i.e. state is finished/unknown) is
    # the only gate that remains.
    result = monitor.dismiss([{"id": "stuck", "key": "a" * 64}])
    assert result["dismissed"] == ["stuck"]
    assert result["skipped"] == []
    assert len(monitor.snapshot()["sessions"]) == 0


def test_dismiss_survives_a_dismiss_key_changing_between_render_and_click_clear_retained_race():
    async def _emit(*_a, **_k) -> None:
        return None

    monitor = FamilyMonitor("TEST", _emit, [row("done", "finished")])
    stale_key = entries(monitor)[0]["key"]
    # Simulate a poll tick landing between the dashboard's last render (which
    # captured stale_key) and the user's click: an identity field the hash
    # depends on shifts (e.g. a fresh runId from a re-observed poll), producing
    # a different current dismissKey even though the family is still eligible
    # (state remains "finished"). Dismiss must still succeed using the stale key.
    monitor.engine.rows["done"] = {**monitor.engine.rows["done"], "runId": "new-run-id"}
    fresh_key = group_families(list(monitor.rows.values()))[0]["dismissKey"]
    assert fresh_key != stale_key
    result = monitor.dismiss([{"id": "done", "key": stale_key}])
    assert result["dismissed"] == ["done"]
    assert result["skipped"] == []
    assert len(monitor.snapshot()["sessions"]) == 0


def test_bulk_clear_finished_never_dismisses_unconfirmed_only_per_row_dismiss_does():
    async def _emit(*_a, **_k) -> None:
        return None

    monitor = FamilyMonitor("TEST", _emit, [row("stuck", "unknown"), row("done", "finished")])
    finished_only = [item for item in group_families(list(monitor.rows.values())) if item["state"] == "finished"]
    result = monitor.dismiss([{"id": item["id"], "key": item["dismissKey"]} for item in finished_only])
    assert result["dismissed"] == ["done"]
    stuck = next(item for item in monitor.snapshot()["sessions"] if item["id"] == "stuck")
    assert stuck["state"] == "unknown"


@pytest.mark.asyncio
async def test_dismissed_run_markers_survive_storage_and_restart_without_replay_or_removing_dedupe(tmp_path: Path):
    ledger = Ledger(str(tmp_path / "ledger.json"))
    await ledger.claim("existing-notification")
    store = SessionStore(str(tmp_path / "sessions.json"))

    async def _emit(key: str, *_a, **_k) -> None:
        await ledger.claim(key)

    monitor = FamilyMonitor("TEST", _emit, [row("p"), row("c", "finished", "p")])
    monitor.dismiss(entries(monitor))
    await store.save(monitor.snapshot()["members"], monitor.dismissed)
    loaded = SessionStore(store.file)

    async def _fail(*_a, **_k) -> None:
        pytest.fail("Historical alert replayed")

    restored = FamilyMonitor("TEST", _fail, await loaded.load(), loaded.dismissed)
    assert len(restored.snapshot()["sessions"]) == 0
    await restored.update([], {"now": 1000})
    assert len(restored.snapshot()["sessions"]) == 0
    assert len(restored.rows) == 2
    assert len(ledger.keys) == 1
    malformed = {"version": 3, "sessions": [row("p")], "dismissed": [{"id": "p", "key": "invalid"}]}
    Path(store.file).write_text(json.dumps(malformed), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid dismissed"):
        await SessionStore(store.file).load()


@pytest.mark.asyncio
@pytest.mark.parametrize("id_", ["p", "c", "new-descendant"])
async def test_new_work_restores_dismissed_family_and_notifies_once_on_new_completion(id_: str):
    alerts: list[dict[str, Any]] = []

    async def _emit(_key: str, alert: dict[str, Any]) -> None:
        alerts.append(alert)

    monitor = FamilyMonitor("TEST", _emit, [row("p"), row("c", "finished", "p")])
    old = entries(monitor)
    monitor.dismiss(old)
    events = EventState()
    event(events, "assistant.turn_start", "start", {"interactionId": f"new-{id_}", "turnId": "0"})
    parent = None if id_ == "p" else "p"
    working = await monitor.update([sample(id_, events, parent)], {"now": 1000})
    assert working["sessions"][0]["state"] == "working"
    assert len(monitor.dismissed) == 0
    event(events, "assistant.message", "answer", {"phase": "final_answer", "turnId": "0", "toolRequests": []})
    event(events, "assistant.turn_end", "end", {"turnId": "0"})
    done = await monitor.update([sample(id_, events, parent, busy=False)], {"now": 2000})
    assert done["sessions"][0]["state"] == "finished"
    # NOTE: dismiss() no longer compares the submitted key's value, only current
    # eligibility -- so this stale pre-new-completion key now succeeds (it did
    # not before). The alert-emission assertion below is unaffected: the
    # "finished" alert already fired from the `done =` update above (armed via
    # the earlier "working" transition), and this dismiss() call does not emit
    # alerts itself.
    assert monitor.dismiss(old)["dismissed"] == ["p"]
    await monitor.update([sample(id_, events, parent, busy=False)], {"now": 3000})
    assert len([alert for alert in alerts if alert["kind"] == "finished"]) == 1


@pytest.mark.asyncio
async def test_fresh_state_serialized_guard_skips_resumed_family_but_dismisses_unchanged_bulk_entries():
    async def _emit(*_a, **_k) -> None:
        return None

    monitor = FamilyMonitor("TEST", _emit, [row("p"), row("other")])
    request = entries(monitor)
    events = EventState()
    event(events, "assistant.turn_start", "start", {"interactionId": "resumed", "turnId": "0"})
    refreshed = False
    persisted = False

    async def _refresh() -> None:
        nonlocal refreshed
        refreshed = True
        await monitor.update([sample("p", events)], {"now": 1000})

    async def _persist() -> None:
        nonlocal persisted
        persisted = True

    actions = MonitorActions(monitor, _refresh, _persist, lambda: True)
    release_future = asyncio.get_event_loop().create_future()

    async def _blocking() -> None:
        await release_future

    ongoing = actions.run(_blocking)
    await asyncio.sleep(0)
    dismissal = actions.dismiss(request)
    assert refreshed is False
    release_future.set_result(None)
    await ongoing
    result = await dismissal
    assert result["dismissed"] == ["other"]
    assert result["skipped"][0]["id"] == "p"
    assert persisted is True
    assert monitor.snapshot()["sessions"][0]["state"] == "working"


@pytest.mark.asyncio
async def test_failed_refresh_or_persistence_cannot_silently_dismiss_malformed_input_rejected():
    async def _emit(*_a, **_k) -> None:
        return None

    monitor = FamilyMonitor("TEST", _emit, [row("p")])
    request = entries(monitor)

    async def _refresh_ok() -> None:
        return None

    async def _persist_fail_assert() -> None:
        pytest.fail("persist should not be called")

    offline = MonitorActions(monitor, _refresh_ok, _persist_fail_assert, lambda: False)
    with pytest.raises(DismissError) as excinfo:
        await offline.dismiss(request)
    assert excinfo.value.status == 503

    async def _persist_disk_failure() -> None:
        raise Exception("Disk failure")

    failure = MonitorActions(monitor, _refresh_ok, _persist_disk_failure, lambda: True)
    with pytest.raises(Exception, match="Disk failure"):
        await failure.dismiss(request)
    assert len(monitor.dismissed) == 0

    async def _persist_noop() -> None:
        return None

    valid = MonitorActions(monitor, _refresh_ok, _persist_noop, lambda: True)
    with pytest.raises(DismissError) as excinfo2:
        await valid.dismiss([])
    assert excinfo2.value.status == 400

    async def _one_chunk_reader(chunks: list[bytes]):
        for chunk in chunks:
            yield chunk

    with pytest.raises(DismissError) as excinfo3:
        await read_dismiss_entries(_one_chunk_reader([b"{"]))
    assert excinfo3.value.status == 400

    with pytest.raises(DismissError) as excinfo4:
        await read_dismiss_entries(_one_chunk_reader([bytes(1048577)]))
    assert excinfo4.value.status == 413

    body = json.dumps({"entries": request}).encode("utf-8")
    assert await read_dismiss_entries(_one_chunk_reader([body])) == request


def test_dormant_linked_names_and_missing_metadata_unobserved_nested_without_changing_completion():
    members = [row("p"), row("c", "finished", "p")]
    metadata = [
        {"id": "dormant", "parentId": "c", "title": "Full dormant grandchild name", "detail": "Execution not observed"},
        {"id": "missing", "parentId": "dormant", "title": "Name unavailable (missing)", "hierarchyIssue": "Metadata unavailable", "detail": "Not observed"},
        {"id": "unrelated", "parentId": None, "title": "Never import unrelated history"},
    ]
    family = group_families(members, metadata)[0]
    assert family["state"] == "finished"
    assert [(item["id"], item["depth"]) for item in family["relatives"]] == [
        ("p", 0), ("c", 1), ("dormant", 2), ("missing", 3),
    ]
    assert family["relatives"][2]["state"] == "unobserved"
    assert family["dismissKey"] == group_families(members)[0]["dismissKey"]


@pytest.mark.asyncio
async def test_restart_does_not_flash_dismissed_child_only_family_while_idle_ancestor_baselined():
    retained = [{**row("p", "idle"), "contextOnly": True}, row("c", "finished", "p")]

    async def _emit(*_a, **_k) -> None:
        return None

    monitor = FamilyMonitor("TEST", _emit, retained)
    monitor.engine.rows = {item["id"]: item for item in retained}
    monitor.dismiss(entries(monitor))

    async def _fail(*_a, **_k) -> None:
        pytest.fail("No historical alert")

    restored = FamilyMonitor("TEST", _fail, retained, monitor.dismissed)
    assert len(restored.snapshot()["sessions"]) == 0
    await restored.update(
        [{**sample("p", EventState(), None, busy=False), "contextOnly": True}], {"now": 1000}
    )
    assert len(restored.snapshot()["sessions"]) == 0
    await restored.update(
        [{**sample("p", EventState(), None, busy=False), "alive": False, "contextOnly": True}], {"now": 2000}
    )
    assert restored.snapshot()["sessions"][0]["state"] == "unknown"
