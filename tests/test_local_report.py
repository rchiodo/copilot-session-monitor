"""Tests for pymonitor.local_report (port of src/local-report.mjs).

Covers poll_local's catch-all error path: any observation failure must
surface a useful message, not just a bare exception class name.
"""
from __future__ import annotations

from typing import Any

import pytest

from pymonitor.families import FamilyMonitor
from pymonitor.local_report import poll_local


async def _noop_notify(key: str, alert: dict[str, Any]) -> None:
    pass


def _ctx() -> dict[str, Any]:
    return {"monitor": FamilyMonitor("TEST", _noop_notify, []), "source": object()}


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
    class _FakeSource:
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
    class _FakeSource:
        async def poll(self) -> dict[str, Any]:
            raise RuntimeError()

    polled = await poll_local({"monitor": FamilyMonitor("TEST", _noop_notify, []), "source": _FakeSource()})
    assert polled["healthy"] is False
    assert polled["issues"] == ["Local observation unavailable (RuntimeError); no completion inferred"]
