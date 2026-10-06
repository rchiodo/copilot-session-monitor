"""Port of src/local-report.mjs.

Shared local Copilot-session observation logic used both by the standalone
watcher (which reports over HTTPS to a paired collector) and the collector's
own in-process self-source (which observes its own machine with no network
hop at all). Keeping this in one place means both call sites apply the exact
same conservative completion rules.
"""
from __future__ import annotations

import os
from typing import Any, Awaitable, Callable

from .engine import digest
from .families import FamilyMonitor
from .protocol import observed_metadata
from .source import LocalSource, ProcessSnapshot

NoticeFn = Callable[[dict[str, Any]], None]


def create_local_observer(
    label: str,
    retained: list[dict[str, Any]],
    process_snapshot: ProcessSnapshot,
    on_notice: NoticeFn,
) -> dict[str, Any]:
    async def _notify(key: str, alert: dict[str, Any]) -> None:
        on_notice({"key": digest(key), "familyId": alert["familyId"], "kind": alert["kind"]})

    monitor = FamilyMonitor(label, _notify, retained)
    source = LocalSource(os.path.join(os.path.expanduser("~"), ".copilot"), process_snapshot)
    for id_ in monitor.rows.keys():
        source.tracked.add(id_)
    return {"monitor": monitor, "source": source}


async def poll_local(ctx: dict[str, Any], forced_gap_reason: str | None = None) -> dict[str, Any]:
    source: LocalSource = ctx["source"]
    monitor: FamilyMonitor = ctx["monitor"]
    issues: list[str] = []
    healthy = True
    samples: list[dict[str, Any]] = []
    try:
        if forced_gap_reason:
            raise RuntimeError(forced_gap_reason)
        observed = await source.poll()
        samples = observed["samples"]
        result = await monitor.update(observed["samples"], {"relatives": observed["relatives"]})
        issues = observed["issues"]
        healthy = not result["gap"]
        for id_ in monitor.rows.keys():
            source.tracked.add(id_)
        source.release_idle(set(monitor.rows.keys()))
    except Exception as error:  # noqa: BLE001 -- mirrors JS catch-all; any observation failure forces a gap.
        healthy = False
        label = str(error) or type(error).__name__
        issues = [f"Local observation unavailable ({label}); no completion inferred"]
        result = await monitor.update([], {"healthy": False, "reason": issues[0]})
    return {"result": result, "issues": issues, "healthy": healthy, "samples": samples}


def local_report_payload(monitor: FamilyMonitor, polled: dict[str, Any], notices: list[dict[str, Any]]) -> dict[str, Any]:
    result = polled["result"]
    snapshot = {**result, "relatives": monitor.relatives}
    return {
        **observed_metadata(snapshot, polled["samples"], monitor.observed),
        "healthy": polled["healthy"],
        "issues": polled["issues"],
        "notices": notices,
    }
