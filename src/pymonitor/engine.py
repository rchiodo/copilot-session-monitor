"""Port of src/engine.mjs.

This is the core status state machine. It houses the fixes for several
hard-won regressions (see docs/porting-notes.md for commit references):

  * d2e99f3 / ff1003c -- a session showing "unconfirmed" after it had
    already finished, caused by a poll racing a process restart; fixed by
    the `retain(... 'finished' row refresh-without-reinference ...)` branch
    and by `observed` tracking `terminalAtObservation` per runId.
  * 37a702a -- family/background "infection": a sibling session's
    unresolved background work must not retroactively flip an unrelated
    session's state; `BackgroundWork` snapshots are scoped per-EventState,
    and `lose()`/`retain()` only act on same-id evidence.
  * e15890a -- "zombie row": a row that is no longer present in samples
    must not be silently left in a stale non-terminal state; the
    post-loop sweep below demotes any present-less working/waiting/idle
    row to 'unknown'.
  * 721b02c -- gap/offline detection: `update()` treats a >15s poll gap
    (sleep/pause) identically to an explicit unhealthy observation,
    discarding completion authority rather than guessing.
  * 31b0694 -- reconnect-after-lease: a freshly reconnected watcher must
    not retroactively mark an in-progress run as unhealthy merely because
    the lease briefly lapsed; this is handled by `healthy`/`gap` being
    caller-supplied (lease logic lives in the Phase 2 protocol layer, not
    here), so lease flapping is invisible to the engine as long as the
    reporter keeps observing within the 15s window.

This module intentionally stays dict-based (mirroring the original's
plain JS objects) rather than being rewritten as a bundle of dataclasses:
the goal is byte-for-byte traceable logic, not idiomatic Python.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

_KEY_RE = re.compile(r"^[a-f0-9]{64}$")
_VALID_STATES = {"working", "finished", "waiting", "error", "unknown", "idle"}
_ALERT_KINDS = {"finished", "waiting", "error", "warning"}

_ROW_FIELDS = [
    "id", "title", "machine", "source", "state", "detail", "activity", "runId",
    "firstObservedAt", "startedAt", "lastEventAt", "lastResponseAt", "finishedAt",
    "parentId", "hierarchyIssue", "contextOnly", "lastAlert",
]


def _now_iso(now_ms: float) -> str:
    return datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_date(value: str | None) -> float:
    if not value:
        return float("-inf")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        return float("-inf")


def _is_finite_date(value: Any) -> bool:
    return isinstance(value, str) and _parse_date(value) != float("-inf")


def _coalesce(*values: Any) -> Any:
    """Mirrors JS `??`: returns the first value that is not None (unlike `or`,
    an explicit False/0/"" is a real value and stops the chain)."""
    for value in values:
        if value is not None:
            return value
    return None


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sort_sessions(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    def key(row: dict[str, Any]):
        ts = row.get("lastResponseAt") or row.get("firstObservedAt")
        return (-_parse_date(ts), row["id"])

    return sorted(rows, key=key)


def unavailable_sessions(
    rows: Iterable[dict[str, Any]],
    reason: str,
    process_started_at_ms: float | None = None,
) -> list[dict[str, Any]]:
    out = []
    for row in rows:
        result = dict(row)
        if row.get("state") in ("working", "waiting", "idle"):
            result["state"] = "unknown"
            result["detail"] = reason
            result["finishedAt"] = None
        elif (
            row.get("state") == "finished"
            and process_started_at_ms is not None
            and _parse_date(row.get("finishedAt")) < process_started_at_ms
        ):
            # A "finished" row restored from disk that completed before *this*
            # engine instance even started (e.g. a cold host restart loading a
            # session that finished days ago). Without this, such a row never
            # passes through FamilyMonitor's own restore-staleness check at
            # all -- that check only looks at FamilyMonitor's own previously
            # persisted families, not at rows freshly supplied here on the
            # very first tick. Treat it the same as a stale working/waiting/
            # idle row: unconfirmed until genuinely re-observed.
            result["state"] = "unknown"
            result["detail"] = reason
            result["finishedAt"] = None
        if row.get("members"):
            result["members"] = unavailable_sessions(row["members"], reason, process_started_at_ms)
            if row.get("relatives"):
                result["relatives"] = unavailable_sessions(row["relatives"], reason, process_started_at_ms)
            result["runningCount"] = 0
        out.append(result)
    return out


def _stored_row(row: dict[str, Any]) -> dict[str, Any]:
    row = {"parentId": None, "hierarchyIssue": None, "contextOnly": False, "lastAlert": None, **row}
    for key in ("id", "title", "machine", "source", "state", "detail", "activity"):
        if not isinstance(row.get(key), str):
            raise ValueError(f"Invalid retained session field: {key}")
    if row["state"] not in _VALID_STATES or not (row.get("runId") is None or isinstance(row.get("runId"), str)):
        raise ValueError("Invalid retained session state")
    for key in ("firstObservedAt", "startedAt", "lastEventAt", "lastResponseAt", "finishedAt"):
        if key != "firstObservedAt" and row.get(key) is None:
            continue
        if not isinstance(row.get(key), str) or not _is_finite_date(row.get(key)):
            raise ValueError(f"Invalid retained session timestamp: {key}")
    if (row["state"] == "finished") != (row.get("finishedAt") is not None):
        raise ValueError("Invalid retained completion time")
    for key in ("parentId", "hierarchyIssue"):
        if row.get(key) is not None and not isinstance(row.get(key), str):
            raise ValueError(f"Invalid retained {key}")
    if not isinstance(row.get("contextOnly"), bool):
        raise ValueError("Invalid retained context flag")
    if row.get("lastAlert") is not None:
        alert = row["lastAlert"]
        if (
            alert.get("sessionId") != row["id"]
            or alert.get("kind") not in _ALERT_KINDS
            or not isinstance(alert.get("key"), str)
            or not isinstance(alert.get("message"), str)
            or not isinstance(alert.get("at"), str)
            or not _is_finite_date(alert.get("at"))
        ):
            raise ValueError("Invalid retained parent alert")
        row["lastAlert"] = {key: alert[key] for key in ("sessionId", "key", "kind", "message", "at")}
    return {key: row.get(key) for key in _ROW_FIELDS}


class Ledger:
    """Mirrors engine.mjs's Ledger class: a durable at-most-once notification claim set."""

    def __init__(self, file: str) -> None:
        self.file = file
        self.keys: set[str] = set()

    async def load(self) -> None:
        try:
            data = json.loads(Path(self.file).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        keys = data.get("keys")
        if data.get("version") != 1 or not isinstance(keys, list) or not all(
            isinstance(key, str) and _KEY_RE.match(key) for key in keys
        ):
            raise ValueError("Invalid notification ledger")
        self.keys = set(keys)

    async def claim(self, key: str) -> bool:
        return await self.claim_digest(digest(key))

    async def claim_digest(self, hash_: str) -> bool:
        if not _KEY_RE.match(hash_):
            raise TypeError("Invalid notification digest")
        return await self._claim_once(hash_)

    async def _claim_once(self, hash_: str) -> bool:
        if hash_ in self.keys:
            return False
        keys = (list(self.keys) + [hash_])[-4096:]
        path = Path(self.file)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(f"{self.file}.tmp")
        tmp.write_text(json.dumps({"version": 1, "keys": keys}), encoding="utf-8")
        os.replace(tmp, path)
        self.keys = set(keys)
        return True


class SessionStore:
    """Mirrors engine.mjs's SessionStore class: durable retained-row persistence across restarts."""

    def __init__(self, file: str) -> None:
        self.file = file
        self.serialized: str | None = None
        self.dismissed: dict[str, str] = {}

    async def load(self) -> list[dict[str, Any]]:
        try:
            data = json.loads(Path(self.file).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        if data.get("version") not in (1, 2, 3) or not isinstance(data.get("sessions"), list):
            raise ValueError("Invalid retained session store")
        rows = [_stored_row(row) for row in data["sessions"]]
        if len({row["id"] for row in rows}) != len(rows):
            raise ValueError("Duplicate retained session IDs")
        dismissed = data.get("dismissed", []) if data.get("version") == 3 else []
        if (
            not isinstance(dismissed, list)
            or any(
                not isinstance(entry.get("id"), str)
                or not isinstance(entry.get("key"), str)
                or not _KEY_RE.match(entry["key"])
                for entry in dismissed
            )
            or len({entry["id"] for entry in dismissed}) != len(dismissed)
        ):
            raise ValueError("Invalid dismissed family markers")
        self.dismissed = {entry["id"]: entry["key"] for entry in dismissed}
        self.serialized = json.dumps({"version": data["version"], "sessions": sort_sessions(rows)})
        return rows

    async def save(self, rows: Iterable[dict[str, Any]], dismissed: dict[str, str] | None = None) -> None:
        dismissed = self.dismissed if dismissed is None else dismissed
        serialized = json.dumps(
            {
                "version": 3,
                "sessions": [_stored_row(row) for row in sort_sessions(rows)],
                "dismissed": [{"id": id_, "key": key} for id_, key in dismissed.items()],
            }
        )
        if serialized == self.serialized:
            return
        path = Path(self.file)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(f"{self.file}.tmp")
        tmp.write_text(serialized, encoding="utf-8")
        os.replace(tmp, path)
        self.serialized = serialized


EmitFn = Callable[[str, dict[str, Any]], Awaitable[None]]


class MonitorEngine:
    """Mirrors engine.mjs's MonitorEngine class -- the per-machine status state machine."""

    def __init__(
        self,
        machine: str,
        emit: EmitFn,
        retained: Iterable[dict[str, Any]] = (),
        process_started_at_ms: float | None = None,
    ) -> None:
        self.machine = machine
        self.emit = emit
        self.observed: dict[str, dict[str, Any]] = {}
        self.rows: dict[str, dict[str, Any]] = {
            row["id"]: row
            for row in unavailable_sessions(
                list(retained), "Monitor restarted; current run status is unconfirmed", process_started_at_ms
            )
        }
        self.last_poll: float | None = None
        self.baseline = True

    def snapshot(self, gap: bool = False) -> dict[str, Any]:
        sessions = sort_sessions(self.rows.values())
        return {
            "sessions": sessions,
            "active": [row for row in sessions if row["state"] == "working"],
            "attention": [row for row in sessions if row["state"] in ("waiting", "error", "unknown")],
            "gap": gap,
        }

    async def update(
        self,
        samples: Iterable[dict[str, Any]],
        *,
        healthy: bool = True,
        now: float | None = None,
        reason: str = "Observation unavailable",
    ) -> dict[str, Any]:
        import time

        now = time.time() * 1000 if now is None else now
        gap = self.last_poll is not None and (now - self.last_poll > 15000 or now < self.last_poll)
        self.last_poll = now
        if not healthy or gap:
            detail = "Observation gap (sleep or pause)" if gap else reason
            self.rows = {
                row["id"]: row for row in unavailable_sessions(list(self.rows.values()), detail)
            }
            for id_, previous in self.observed.items():
                await self.emit(
                    f"{id_}:{previous['runId']}:offline",
                    {
                        "sessionId": id_,
                        "kind": "warning",
                        "title": previous["card"]["title"],
                        "message": f"{detail}. Completion was not confirmed.",
                    },
                )
            self.observed.clear()
            self.baseline = True
            return self.snapshot(gap)

        present: set[str] = set()
        for sample in samples:
            id_ = sample["id"]
            e = sample.get("events")
            busy = bool(sample.get("busy")) or bool(e and e.get("backgroundCount") and not sample.get("activityUnconfirmed"))
            unresolved = bool(
                sample.get("activityUnconfirmed") or sample.get("completionUnconfirmed") or (e and e.get("backgroundUnconfirmed"))
            )
            present.add(id_)
            previous = self.observed.get(id_)
            saved = self.rows.get(id_)
            if saved is not None and "parentId" in sample:
                saved = {
                    **saved,
                    "parentId": sample["parentId"],
                    "hierarchyIssue": sample.get("hierarchyIssue"),
                    "title": sample["title"],
                }
                self.rows[id_] = saved

            response_times = [t for t in (saved.get("lastResponseAt") if saved else None, e.get("lastResponseAt") if e else None) if t]
            card = {
                "id": id_,
                "title": sample["title"],
                "machine": self.machine,
                "source": sample["source"],
                "runId": _coalesce(e.get("runId") if e else None, saved.get("runId") if saved else None),
                "startedAt": _coalesce(e.get("startedAt") if e else None, saved.get("startedAt") if saved else None),
                "lastEventAt": _coalesce(e.get("lastEventAt") if e else None, saved.get("lastEventAt") if saved else None),
                "lastResponseAt": max(response_times, key=_parse_date) if response_times else None,
                "firstObservedAt": _coalesce(saved.get("firstObservedAt") if saved else None, _now_iso(now)),
                "activity": _coalesce(e.get("activity") if e else None, saved.get("activity") if saved else None, "Agent running"),
                "parentId": _coalesce(sample.get("parentId"), saved.get("parentId") if saved else None),
                "hierarchyIssue": _coalesce(sample.get("hierarchyIssue"), saved.get("hierarchyIssue") if saved else None),
                "contextOnly": _coalesce(saved.get("contextOnly") if saved else None, sample.get("contextOnly"), False),
                "lastAlert": _coalesce(saved.get("lastAlert") if saved else None, None),
            }

            def retain(state: str, detail: str, finished_at: str | None = None, _card=card, _id=id_) -> None:
                self.rows[_id] = {
                    **_card,
                    "state": state,
                    "detail": detail,
                    "finishedAt": finished_at,
                    "contextOnly": False if state == "working" else _card["contextOnly"],
                }

            async def lose(detail: str, _card=card, _id=id_, _saved=saved, _previous=previous) -> None:
                if (
                    (
                        _saved
                        and (
                            _saved["state"] != "finished"
                            or busy
                            or unresolved
                            or (e and e.get("backgroundCount"))
                            or (e and e.get("runId") != _saved["runId"])
                            or _parse_date(e.get("lastExecutionAt") if e else None) > _parse_date(_saved["lastEventAt"])
                        )
                    )
                    or sample.get("contextOnly")
                    or busy
                    or unresolved
                ):
                    retain("unknown", detail, _card=_card, _id=_id)
                if _previous:
                    await self.emit(
                        f"{_id}:{_previous['runId']}:offline",
                        {
                            "sessionId": _id,
                            "kind": "warning",
                            "title": _card["title"],
                            "message": f"{detail}. Completion was not confirmed.",
                        },
                    )
                self.observed.pop(_id, None)

            if (
                not sample.get("alive")
                or not e
                or sample.get("readError")
                or e.get("closed")
                or e.get("replaced")
                or (unresolved and not busy)
                or (previous and previous.get("owner") != sample.get("owner"))
            ):
                detail = (
                    sample.get("readError")
                    or sample.get("activityUnconfirmed")
                    or sample.get("completionUnconfirmed")
                    or (
                        "Outstanding background work has unconfirmed status"
                        if unresolved
                        else ("Event file rotated; rebaselining" if (e and e.get("replaced")) else "Session process unavailable or changed")
                    )
                )
                await lose(detail)
                continue

            if not busy and not previous and card["contextOnly"]:
                gated = e.get("waiting") or e.get("error") or sample.get("interrupted") or e.get("partial")
                retain(
                    "unknown" if gated else "idle",
                    "Related session has unresolved evidence; current status is unconfirmed"
                    if gated
                    else "Parent/ancestor observed idle; no completion alert inferred",
                )
                continue

            if e.get("waiting"):
                if previous or busy or (saved and saved["state"] == "waiting"):
                    retain("waiting", e["waiting"]["kind"])
                elif saved:
                    retain("unknown", "A saved input gate exists; current waiting status is unconfirmed")
                if not self.baseline and previous:
                    await self.emit(
                        f"{id_}:{e['waiting']['id']}:waiting",
                        {
                            "sessionId": id_,
                            "kind": "waiting",
                            "title": card["title"],
                            "message": f"{e['waiting']['kind']}. The run is not finished.",
                        },
                    )
                continue

            if e.get("error") or sample.get("interrupted"):
                detail = _coalesce((e.get("error") or {}).get("kind"), "Run interrupted")
                if saved or busy:
                    retain("error", detail)
                if not self.baseline and (previous or (saved and saved["state"] == "finished" and e.get("runId") == saved["runId"])):
                    error_id = _coalesce(
                        (e.get("error") or {}).get("id"),
                        (previous or {}).get("runId"),
                        e.get("runId"),
                    )
                    await self.emit(
                        f"{id_}:{error_id}:error",
                        {
                            "sessionId": id_,
                            "kind": "error",
                            "title": card["title"],
                            "message": f"{detail}. This is not a successful completion.",
                        },
                    )
                self.observed.pop(id_, None)
                continue

            # Refresh response metadata without inferring a new completion after restart.
            if (
                not busy
                and not previous
                and saved
                and saved["state"] == "finished"
                and e.get("runId") == saved["runId"]
                and e.get("terminal")
                and (not e.get("lastExecutionAt") or _parse_date(e.get("lastExecutionAt")) <= _parse_date(saved["lastEventAt"]))
            ):
                self.rows[id_] = {**saved, **card}
                continue

            if busy and e.get("runId") and not e.get("partial"):
                if sample.get("source") == "CLI (activity only)" and not e.get("activeTurn"):
                    if saved:
                        retain("unknown", "Model turn ended; full CLI run status is unavailable")
                    self.observed.pop(id_, None)
                    continue
                terminal_at_observation = (
                    previous.get("terminalAtObservation")
                    if previous and previous.get("runId") == e.get("runId")
                    else (e.get("terminal") or {}).get("id")
                )
                retain("working", e["activity"])
                self.observed[id_] = {
                    "runId": e["runId"],
                    "owner": sample.get("owner"),
                    "card": card,
                    "terminalAtObservation": terminal_at_observation,
                }
            elif busy and e.get("partial") and previous and saved and saved["state"] == "working":
                # Retain existing execution evidence while the next JSONL record is incomplete.
                pass
            elif not busy and previous and sample.get("source") in ("Copilot desktop", "CLI (activity only)"):
                if (
                    e.get("terminal")
                    and not e.get("partial")
                    and e.get("runId") == previous.get("runId")
                    and e["terminal"]["id"] != previous.get("terminalAtObservation")
                ):
                    await self.emit(
                        f"{id_}:{e['runId']}:finished",
                        {
                            "sessionId": id_,
                            "kind": "finished",
                            "title": card["title"],
                            "message": "Current agent run finished. This does not mean the entire task or PR succeeded.",
                        },
                    )
                    retain("finished", "Current run finished; this is not task or PR success", _now_iso(now))
                    self.observed.pop(id_, None)
                else:
                    retain("unknown", "Not running; waiting for explicit completion evidence")
            elif saved:
                if saved["state"] != "error":
                    retain(
                        "unknown",
                        "Current execution evidence is incomplete" if busy else "Not running; full run completion was not observed",
                    )
                self.observed.pop(id_, None)

        for id_, row in list(self.rows.items()):
            if id_ not in present and row["state"] in ("working", "waiting", "idle"):
                self.rows[id_] = {**row, "state": "unknown", "detail": "Session no longer observable", "finishedAt": None}

        for id_, previous in list(self.observed.items()):
            if id_ not in present:
                await self.emit(
                    f"{id_}:{previous['runId']}:offline",
                    {
                        "sessionId": id_,
                        "kind": "warning",
                        "title": previous["card"]["title"],
                        "message": "Session no longer observable. Completion was not confirmed.",
                    },
                )
                self.observed.pop(id_, None)

        self.baseline = False
        return self.snapshot()
