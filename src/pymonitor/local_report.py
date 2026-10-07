"""Port of src/local-report.mjs.

Shared local Copilot-session observation logic used both by the standalone
watcher (which reports over HTTPS to a paired collector) and the collector's
own in-process self-source (which observes its own machine with no network
hop at all). Keeping this in one place means both call sites apply the exact
same conservative completion rules.
"""
from __future__ import annotations

import os
import time
from typing import Any, Awaitable, Callable

from .engine import digest
from .families import FamilyMonitor
from .protocol import observed_metadata
from .source import LocalSource, ProcessSnapshot

NoticeFn = Callable[[dict[str, Any]], None]

# Tolerate this many *consecutive* poll() failures before forcing every
# tracked family to "unknown" via monitor.update([], healthy=False).
#
# source.py's LocalSource.poll() raises whenever tray_native.py's process
# snapshot (refreshed roughly every 2s via call_soon_threadsafe) is more
# than 8000ms stale, which can happen for a single poll_local() tick (polls
# run every ~1.5s) if the asyncio event loop is briefly busy -- a transient
# hiccup, not a real observation outage. Forcing healthy=False straight into
# the engine bypasses its own >15s gap tolerance (721b02c) and flips every
# already-settled family (including "finished" ones) to "unknown" for that
# one cycle, which in turn changes families.py's dismissKey and silently
# breaks "Clear retained" for anything dismissed around that moment.
#
# Escalating only after a short run of consecutive failures preserves a
# single bad tick's "previous confirmed state" while still forcing the
# unavailable-sweep if the underlying problem actually persists.
_TRANSIENT_FAILURE_GRACE_CYCLES = 2


def create_local_observer(
    label: str,
    retained: list[dict[str, Any]],
    process_snapshot: ProcessSnapshot,
    on_notice: NoticeFn,
) -> dict[str, Any]:
    async def _notify(key: str, alert: dict[str, Any]) -> None:
        on_notice({"key": digest(key), "familyId": alert["familyId"], "kind": alert["kind"]})

    # Hide any "finished" family restored from a previous process run that
    # completed before *this* run even started -- the Finished column should
    # only show work observed during the current run, not stale completions
    # that could otherwise linger on screen indefinitely (see
    # FamilyMonitor.__init__'s process_started_at_ms handling for how this
    # stays consistent with the existing dismiss/re-surface mechanism).
    monitor = FamilyMonitor(label, _notify, retained, process_started_at_ms=time.time() * 1000)
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
    if forced_gap_reason:
        # An explicit, caller-requested discard (e.g. server.py/watcher.py's
        # `local_reset`, set after a pairing/connection change) -- this is a
        # deliberate "stop trusting prior state" signal, not a transient
        # observation glitch, so it must always escalate immediately rather
        # than going through the consecutive-failure grace window below.
        healthy = False
        issues = [f"Local observation unavailable ({forced_gap_reason}); no completion inferred"]
        source.consecutive_failures = 0
        result = await monitor.update([], {"healthy": False, "reason": issues[0]})
        return {"result": result, "issues": issues, "healthy": healthy, "samples": samples}
    try:
        observed = await source.poll()
        samples = observed["samples"]
        result = await monitor.update(observed["samples"], {"relatives": observed["relatives"]})
        issues = observed["issues"]
        healthy = not result["gap"]
        source.consecutive_failures = 0
        for id_ in monitor.rows.keys():
            source.tracked.add(id_)
        source.release_idle(set(monitor.rows.keys()))
    except Exception as error:  # noqa: BLE001 -- mirrors JS catch-all; any observation failure forces a gap.
        healthy = False
        label = str(error) or type(error).__name__
        issues = [f"Local observation unavailable ({label}); no completion inferred"]
        source.consecutive_failures += 1
        if source.consecutive_failures > _TRANSIENT_FAILURE_GRACE_CYCLES:
            result = await monitor.update([], {"healthy": False, "reason": issues[0]})
        else:
            # Still within the grace window: report the failure for this
            # cycle's "issues"/"healthy" fields, but leave every family's
            # confirmed state exactly as it was -- don't let one transient
            # tick discard completion authority (see module docstring above
            # and the _TRANSIENT_FAILURE_GRACE_CYCLES comment).
            result = monitor.snapshot()
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
