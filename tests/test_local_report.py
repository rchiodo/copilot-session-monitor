"""Tests for pymonitor.local_report (port of src/local-report.mjs).

Covers poll_local's catch-all error path: any observation failure must
surface a useful message, not just a bare exception class name. Also covers
the consecutive-failure grace window that keeps a single transient poll
glitch from forcing every already-settled family to "unknown" (see
_TRANSIENT_FAILURE_GRACE_CYCLES in local_report.py).
"""
from __future__ import annotations

from typing import Any

import pytest

from pymonitor.families import FamilyMonitor
from pymonitor.local_report import _TRANSIENT_FAILURE_GRACE_CYCLES, poll_local

from .test_families import finish, run, sample


async def _noop_notify(key: str, alert: dict[str, Any]) -> None:
    pass


class _FakeSourceBase:
    """Minimal LocalSource stand-in: just enough attribute surface for
    poll_local's consecutive-failure bookkeeping (plus the `tracked`/
    `release_idle` surface poll_local's success path also touches)."""

    def __init__(self) -> None:
        self.consecutive_failures = 0
        self.tracked: set[str] = set()

    def release_idle(self, keep_ids: set[str]) -> None:
        pass


def _ctx() -> dict[str, Any]:
    return {"monitor": FamilyMonitor("TEST", _noop_notify, []), "source": _FakeSourceBase()}


async def test_forced_gap_reason_message_survives_into_the_issue_text() -> None:
    # forced_gap_reason is surfaced verbatim by the Windows-process-observer
    # race-detection path in watcher.py/collector.py; it must not be reduced
    # to a bare exception class name.
    polled = await poll_local(_ctx(), forced_gap_reason="Windows process observer unavailable")
    assert polled["healthy"] is False
    assert polled["issues"] == [
        "Local observation unavailable (Windows process observer unavailable); no completion inferred"
    ]


async def test_an_exception_with_a_real_message_is_not_reduced_to_its_class_name() -> None:
    class _FakeSource(_FakeSourceBase):
        async def poll(self) -> dict[str, Any]:
            raise RuntimeError("disk read failed: Access is denied")

    polled = await poll_local({"monitor": FamilyMonitor("TEST", _noop_notify, []), "source": _FakeSource()})
    assert polled["healthy"] is False
    assert polled["issues"] == [
        "Local observation unavailable (disk read failed: Access is denied); no completion inferred"
    ]
    # The regression this guards against: previously the label collapsed to
    # a bare class name (e.g. "RuntimeError") and discarded the real message.
    assert "RuntimeError" not in polled["issues"][0]


async def test_an_exception_with_no_message_falls_back_to_its_class_name() -> None:
    class _FakeSource(_FakeSourceBase):
        async def poll(self) -> dict[str, Any]:
            raise RuntimeError()

    polled = await poll_local({"monitor": FamilyMonitor("TEST", _noop_notify, []), "source": _FakeSource()})
    assert polled["healthy"] is False
    assert polled["issues"] == ["Local observation unavailable (RuntimeError); no completion inferred"]


async def test_a_single_transient_poll_failure_does_not_flip_a_working_family_to_unknown() -> None:
    # Reproduces the "unconfirmed" flicker bug: a lone poll() failure (e.g. a
    # momentarily-stale process snapshot) must not force an actively-working
    # family to "unknown" for that cycle. (Note: engine.py's
    # unavailable_sessions() already protects a *standalone* "finished" row
    # from direct mutation -- the real-world flicker hits a row that is still
    # "working"/"waiting"/"idle" at the moment the transient failure lands,
    # discarding genuine progress/completion evidence for that poll cycle.)
    monitor = FamilyMonitor("TEST", _noop_notify, [])
    p = run()
    settled = await monitor.update([sample("p", p)])
    assert settled["sessions"][0]["state"] == "working"

    class _FailingSource(_FakeSourceBase):
        async def poll(self) -> dict[str, Any]:
            raise RuntimeError("Windows process observer unavailable")

    ctx = {"monitor": monitor, "source": _FailingSource()}
    polled = await poll_local(ctx)
    assert polled["healthy"] is False
    assert monitor.rows["p"]["state"] == "working"
    # Still within the grace window after one more failure (grace is >1).
    assert _TRANSIENT_FAILURE_GRACE_CYCLES >= 2
    polled = await poll_local(ctx)
    assert polled["healthy"] is False
    assert monitor.rows["p"]["state"] == "working"


async def test_persistent_poll_failures_past_the_grace_window_still_force_unknown() -> None:
    # The safety-first fallback must still kick in once a failure is
    # genuinely persistent, not just a single transient blip.
    monitor = FamilyMonitor("TEST", _noop_notify, [])
    p = run()
    await monitor.update([sample("p", p)])
    assert monitor.rows["p"]["state"] == "working"

    class _FailingSource(_FakeSourceBase):
        async def poll(self) -> dict[str, Any]:
            raise RuntimeError("Windows process observer unavailable")

    ctx = {"monitor": monitor, "source": _FailingSource()}
    for _ in range(_TRANSIENT_FAILURE_GRACE_CYCLES + 1):
        polled = await poll_local(ctx)
    assert polled["healthy"] is False
    assert monitor.rows["p"]["state"] == "unknown"


async def test_a_successful_poll_resets_the_consecutive_failure_counter() -> None:
    monitor = FamilyMonitor("TEST", _noop_notify, [])
    p = run()
    await monitor.update([sample("p", p)])
    finish(p)
    await monitor.update([sample("p", p, None, {"busy": False})])
    assert monitor.rows["p"]["state"] == "finished"

    source = _FakeSourceBase()
    source.consecutive_failures = _TRANSIENT_FAILURE_GRACE_CYCLES

    async def _poll_ok() -> dict[str, Any]:
        return {"samples": [], "relatives": [], "issues": []}

    source.poll = _poll_ok  # type: ignore[assignment]
    ctx = {"monitor": monitor, "source": source}
    await poll_local(ctx)
    assert source.consecutive_failures == 0
