"""Port of src/collector.mjs.

The highest-risk module in the rewrite: this is the remote-watcher-report
ingestion path (HTTPS collector server) and contains its own independent
copy of the zombie-row-rescue, recovery-votes, and family armed/dismissed
tracking logic. It is a deliberate *parallel* implementation to
``families.FamilyMonitor`` / ``engine.MonitorEngine`` (the local-session,
same-machine, SDK-driven ingestion path) rather than sharing code with it --
``Collector.accept()`` builds its own notice list by walking
``group_families()`` directly, with its own armed/dismissed bookkeeping
scoped per watcher source. Do not try to make this reuse ``FamilyMonitor``.

Preserved bug fixes (see docs/porting-notes.md for commit references and
engine.py's own docstring for the local-session-path equivalents):

  * zombie-row rescue -- a member demoted to 'unknown' by a reconnect
    baseline or a retained-member omission is never silently stuck there
    forever. If its `lastAlert.kind == 'finished'` (a durable breadcrumb
    written once, by the engine's own state machine, at the moment a real
    completion was observed and never cleared by later demotions), a
    subsequent *non-baseline* report rescues it back to 'finished' --- but
    only as an unconfirmed/`completionTracked: False` fact, never as a
    freshly re-armed notification. A row that never finished (still
    'working'/'waiting') has no such breadcrumb and stays lost.
  * recovery votes -- a row already sitting in 'unknown' (demoted by a
    prior gap) requires `RECOVERY_VOTES_REQUIRED` (2) *consecutive*,
    healthy, non-skewed reports of the exact same run (same id + runId +
    startedAt) before being re-trusted back to its reported state. A
    single corroborating report is indistinguishable from the same stale
    report repeating, so one vote is deliberately not enough.
  * baseline vs. continuous trust -- a member already confirmed 'finished'
    is a terminal, settled fact; a continuously-healthy watcher narrowing
    its *reporting window* to exclude a long-quiet session does not make
    that session's completion any less true (`retainedFinish`). But a
    fresh baseline (seq==0, or recovering from unhealthy) carries no live
    guarantee about rows it did not just observe, so the same omission on
    a baseline still surfaces as unconfirmed.
  * family-infection containment -- `invalid` family armed-tracking keys
    off `source.healthy`/`hierarchyIssue`/member-level unconfirmed state
    per *family*, so one family's unconfirmed member cannot retroactively
    arm or suppress notices for an unrelated family on the same source.

This module intentionally stays dict-based (mirroring the original's plain
JS objects / Maps-as-dicts) for the same reason engine.py does: the goal is
byte-for-byte traceable logic, not idiomatic Python.
"""
from __future__ import annotations

import copy
import json
import re
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable

from .engine import digest, sort_sessions
from .families import group_families
from .protocol import HEARTBEAT_MS, fail, metadata, validate_connect, validate_report

__all__ = ["RECOVERY_VOTES_REQUIRED", "Collector"]

# See the module docstring: a row demoted to 'unknown' needs this many
# consecutive, healthy, non-skewed reports of the exact same run before
# recovery-votes re-trust it.
RECOVERY_VOTES_REQUIRED = 2

_KEY_RE = re.compile(r"^[a-f0-9]{64}$")
_STORED_SOURCE_FIELDS = [
    "id", "label", "installationId", "generation", "bootId", "lastSeen", "members", "relatives",
]


def _namespace(source_id: str, id_: str) -> str:
    return f"{source_id}~{id_}"


def _membership(family: dict[str, Any]) -> str:
    return "|".join(sorted(row["id"] for row in family["members"]))


def _unknown(row: dict[str, Any], reason: str) -> dict[str, Any]:
    return {**row, "state": "unknown", "finishedAt": None, "completionTracked": False, "detail": reason}


def _mapped(row: dict[str, Any], source: dict[str, Any]) -> dict[str, Any]:
    def convert(alert: dict[str, Any] | None) -> dict[str, Any] | None:
        if not alert:
            return None
        return {**alert, "sessionId": _namespace(source["id"], alert["sessionId"])}

    result: dict[str, Any] = {
        **row,
        "id": _namespace(source["id"], row["id"]),
        "parentId": _namespace(source["id"], row["parentId"]) if row.get("parentId") else None,
        "aliasOf": _namespace(source["id"], row["aliasOf"]) if row.get("aliasOf") else None,
        "machine": source["label"],
        "reporterId": source["id"],
        "machineTag": f"{source['label']} ({source['id'][:8]})",
        "lastAlert": convert(row.get("lastAlert")),
        "parentAlert": convert(row.get("parentAlert")),
    }
    if row.get("members") is not None:
        result["members"] = [_mapped(member, source) for member in row["members"]]
    if row.get("relatives") is not None:
        result["relatives"] = [_mapped(member, source) for member in row["relatives"]]
    return result


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _iso(now_ms: float) -> str:
    return datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ms(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000


def _invalid_dismiss_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return True
    id_ = entry.get("id")
    key = entry.get("key")
    return not isinstance(id_, str) or len(id_) > 200 or not isinstance(key, str) or not _KEY_RE.match(key)


class Collector:
    """The HTTPS collector's remote-watcher-report ingestion path.

    One `Collector` per collector-mode host; `file` is its durable JSON
    store (full path, not relative to configuration.py's ``.local`` data
    dir -- the original took an arbitrary path too), and `notify` is an
    async callback invoked with ``(key, alert)`` for each de-duplicatable
    notice (mirrors the ``Ledger.claim_digest`` pattern used elsewhere).
    """

    def __init__(self, file: str, notify: Callable[[str, dict[str, Any]], Awaitable[None]]) -> None:
        self.file = file
        self.notify = notify
        self.sources: dict[str, dict[str, Any]] = {}
        self.dismissed: dict[str, str] = {}
        self.pairings: dict[str, dict[str, Any]] = {}

    async def load(
        self,
        config: dict[str, Any],
        legacy: Iterable[dict[str, Any]] = (),
        dismissed: dict[str, str] | None = None,
    ) -> None:
        legacy = list(legacy)
        dismissed = dict(dismissed or {})
        self.configure(config)
        try:
            raw = Path(self.file).read_text(encoding="utf-8")
        except FileNotFoundError:
            # Matches the original's single try/catch(ENOENT) migration path:
            # any other error (bad JSON, failed validation, duplicate
            # reporter) is a genuine corruption and must propagate, not be
            # swallowed into "treat as brand new store".
            local = next((row for row in config["reporters"] if row.get("legacy")), None)
            if legacy and not local:
                raise ValueError("Legacy state has no local reporter")
            if local and legacy:
                self.sources[local["id"]] = {
                    "id": local["id"], "label": local["label"], "installationId": None, "generation": 0,
                    "bootId": None, "lastSeen": None, **metadata({"members": legacy}),
                    "healthy": False, "lease": None, "armed": {}, "activeRuns": {}, "recoveryVotes": {},
                    "issues": ["Waiting for local watcher after migration"],
                }
                for id_, key in dismissed.items():
                    self.dismissed[_namespace(local["id"], id_)] = key
            await self.save()
            return

        saved = json.loads(raw)
        if saved.get("version") != 1 or not isinstance(saved.get("sources"), list) or not isinstance(saved.get("dismissed"), list):
            raise ValueError("Invalid collector store")
        for source in saved["sources"]:
            if source.get("installationId") is not None or source.get("generation") != 0 or source.get("bootId") is not None:
                validate_connect({
                    "version": 1, "reporterId": source["id"], "installationId": source.get("installationId"),
                    "bootId": source.get("bootId"), "generation": source.get("generation"),
                })
            validate_report({
                "version": 1, "reporterId": source["id"], "lease": "0" * 64, "seq": 1,
                "sentAt": "1970-01-01T00:00:00.000Z", "healthy": False, "issues": [],
                "members": source.get("members"), "relatives": source.get("relatives"), "notices": [],
            })
            if source["id"] in self.sources:
                raise ValueError("Duplicate stored reporter")
            self.sources[source["id"]] = {
                **source, "lease": None, "healthy": False, "armed": {}, "activeRuns": {}, "recoveryVotes": {},
                "issues": ["Collector restarted; waiting for a fresh watcher baseline"],
            }
        for id_, key in saved["dismissed"]:
            if not isinstance(id_, str) or not _KEY_RE.match(key):
                raise ValueError("Invalid collector dismissal")
            self.dismissed[id_] = key

    def configure(self, config: dict[str, Any]) -> None:
        self.pairings = {row["id"]: row for row in config["reporters"]}

    async def save(self) -> None:
        sources = [
            {key: source.get(key) for key in _STORED_SOURCE_FIELDS}
            for source in self.sources.values()
        ]
        payload = {"version": 1, "sources": sources, "dismissed": [[id_, key] for id_, key in self.dismissed.items()]}
        tmp = Path(f"{self.file}.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(Path(self.file))

    def live(self, source: dict[str, Any], now: float) -> bool:
        age = now - source["lastSeen"]
        return bool(source["id"] in self.pairings and source.get("lease") and source["healthy"] and age >= 0 and age < HEARTBEAT_MS)

    async def connect(self, value: dict[str, Any], now: float | None = None) -> dict[str, Any]:
        if now is None:
            now = time.time() * 1000
        validate_connect(value)
        pairing = self.pairings.get(value["reporterId"])
        if not pairing:
            raise fail("Reporter not paired", 403)
        source = self.sources.get(value["reporterId"])
        if source and source.get("installationId") and source["installationId"] != value["installationId"]:
            raise fail("Reporter installation identity conflict", 409)
        if source and (
            value["generation"] < source["generation"]
            or (value["generation"] == source["generation"] and source["bootId"] != value["bootId"])
        ):
            raise fail("Old watcher generation", 409)
        if (
            source and source.get("lease") and source["bootId"] != value["bootId"]
            and now - source["lastSeen"] >= 0 and now - source["lastSeen"] < HEARTBEAT_MS
        ):
            raise fail("Another watcher instance holds this identity", 409)
        new_source = {
            **(source or {}),
            "id": pairing["id"], "label": pairing["label"], "installationId": value["installationId"],
            "generation": value["generation"], "bootId": value["bootId"], "lease": secrets.token_hex(32),
            "seq": 0, "bodyHash": None, "lastSeen": now, "healthy": False, "armed": {}, "activeRuns": {},
            "recoveryVotes": {}, "members": (source or {}).get("members") or [],
            "relatives": (source or {}).get("relatives") or [],
            "issues": ["Awaiting watcher baseline"],
        }
        previous = self.sources.get(new_source["id"])
        self.sources[new_source["id"]] = new_source
        try:
            await self.save()
        except Exception:
            if previous is not None:
                self.sources[new_source["id"]] = previous
            else:
                self.sources.pop(new_source["id"], None)
            raise
        return {"version": 1, "lease": new_source["lease"], "heartbeatMs": HEARTBEAT_MS, "serverTime": _iso(now)}

    async def accept(self, value: dict[str, Any], now: float | None = None) -> dict[str, Any]:
        if now is None:
            now = time.time() * 1000
        validate_report(value)
        source = self.sources.get(value["reporterId"])
        if (
            not source or source["id"] not in self.pairings or source.get("lease") != value["lease"]
            or now - source["lastSeen"] < 0 or now - source["lastSeen"] >= HEARTBEAT_MS
        ):
            raise fail("Lease expired; reconnect and baseline", 409)
        body_hash = digest(json.dumps(value, separators=(",", ":")))
        if value["seq"] == source["seq"] and body_hash == source.get("bodyHash"):
            return {"seq": value["seq"], "duplicate": True}
        if value["seq"] != source["seq"] + 1:
            raise fail("Out-of-order or conflicting report", 409)
        baseline = source["seq"] == 0 or not source["healthy"]
        before = copy.deepcopy(source)
        dismissed_before = dict(self.dismissed)
        skew = abs(now - _parse_ms(value["sentAt"])) > 30000 or any(
            at is not None and _parse_ms(at) > now + 30000
            for row in value["members"]
            for at in (
                row.get("firstObservedAt"), row.get("startedAt"), row.get("lastEventAt"),
                row.get("lastResponseAt"), row.get("finishedAt"), (row.get("lastAlert") or {}).get("at"),
            )
        )
        previous_members = {row["id"]: row for row in source["members"]}
        rows: list[dict[str, Any]] = []
        for row in value["members"]:
            saved = previous_members.pop(row["id"], None)
            last_alert = row.get("lastAlert")
            if saved and saved.get("lastAlert") and (
                not row.get("lastAlert") or _parse_ms(saved["lastAlert"]["at"]) >= _parse_ms(row["lastAlert"]["at"])
            ):
                last_alert = saved["lastAlert"]
            retained_finish = (
                saved is not None and saved.get("state") == "finished" and saved.get("runId") == row.get("runId")
                and saved.get("startedAt") == row.get("startedAt") and saved.get("finishedAt") == row.get("finishedAt")
            )
            observed_finish = (
                not baseline and value["healthy"] and not skew
                and source["activeRuns"].get(row["id"]) == row.get("runId")
            )
            member = {
                **row,
                "firstObservedAt": _coalesce((saved or {}).get("firstObservedAt"), row.get("firstObservedAt")),
                "lastAlert": last_alert,
            }
            if row.get("state") != "finished" or retained_finish or observed_finish:
                source["recoveryVotes"].pop(row["id"], None)
                rows.append(member)
                continue
            # See module docstring (recovery votes): a row already 'unknown'
            # needs RECOVERY_VOTES_REQUIRED consecutive, healthy, non-skewed
            # reports of this exact run before being re-trusted.
            corroborated = (
                not baseline and value["healthy"] and not skew and saved is not None
                and saved.get("state") == "unknown" and saved.get("runId") == row.get("runId")
                and saved.get("startedAt") == row.get("startedAt")
            )
            if not corroborated:
                source["recoveryVotes"].pop(row["id"], None)
            else:
                vote = source["recoveryVotes"].get(row["id"])
                votes = (vote["count"] if vote and vote.get("runId") == row.get("runId") and vote.get("startedAt") == row.get("startedAt") else 0) + 1
                if votes >= RECOVERY_VOTES_REQUIRED:
                    source["recoveryVotes"].pop(row["id"], None)
                    rows.append(member)
                    continue
                source["recoveryVotes"][row["id"]] = {"runId": row.get("runId"), "startedAt": row.get("startedAt"), "count": votes}
            demoted = member
            if saved is None:
                # First-ever sight of this id from this source: the collector
                # has no track record for it at all, so any lastAlert carried
                # through from the watcher's own report is an uncorroborated
                # breadcrumb (e.g. the session legitimately finished on the
                # watcher's machine days before the collector ever started
                # watching it). Clear it so a later "watcher omitted this
                # retained member" report can never zombie-rescue it back to
                # 'finished' using a timestamp the collector itself never
                # confirmed. Rows the collector already has a saved track
                # record for (recovery votes in progress) keep their
                # lastAlert untouched -- that breadcrumb was already subject
                # to this same scrutiny on a prior report.
                demoted = {**member, "lastAlert": None}
            rows.append(_unknown(demoted, "Completion occurred outside continuous collector observation; not confirmed"))

        # See module docstring (baseline vs. continuous trust / zombie-row
        # rescue): a retained member the fresh report omitted.
        tracking_lost = 0
        for row in previous_members.values():
            if not baseline and row.get("state") == "finished":
                rows.append(row)
                continue
            if not baseline and row.get("state") == "unknown" and (row.get("lastAlert") or {}).get("kind") == "finished":
                rows.append({
                    **row, "state": "finished", "finishedAt": row["lastAlert"]["at"],
                    "completionTracked": False, "detail": "Current run finished; this is not task or PR success",
                })
                continue
            tracking_lost += 1
            rows.append(_unknown(row, "Watcher omitted a retained member; completion is unconfirmed"))

        if len(rows) > 5000:
            raise fail("Retained member limit exceeded", 413)
        source["seq"] = value["seq"]
        source["bodyHash"] = body_hash
        source["lastSeen"] = now
        source["members"] = rows
        source["relatives"] = value["relatives"]
        source["healthy"] = value["healthy"] and not skew
        source["issues"] = [
            *value["issues"],
            *(["Watcher clock differs by more than 30 seconds"] if skew else []),
            *(["Watcher omitted retained members; omitted states are unconfirmed"] if tracking_lost else []),
        ]
        if not source["healthy"]:
            source["armed"].clear()
            source["activeRuns"].clear()
            source["recoveryVotes"].clear()
        else:
            for row in rows:
                if row.get("state") == "working" and row.get("completionTracked"):
                    source["activeRuns"][row["id"]] = row.get("runId")
                elif not row.get("completionTracked"):
                    source["activeRuns"].pop(row["id"], None)

        notices: list[dict[str, Any]] = []
        for family in group_families(rows, source["relatives"]):
            family_id = _namespace(source["id"], family["id"])
            if family["state"] == "working" or (family.get("dismissKey") and self.dismissed.get(family_id) != family["dismissKey"]):
                self.dismissed.pop(family_id, None)
            invalid = (
                not source["healthy"] or family.get("hierarchyIssue")
                or any(row["state"] == "unknown" and not row.get("completionTracked") for row in family["members"])
            )
            if invalid:
                source["armed"].pop(family["id"], None)
            events = [notice for notice in value["notices"] if notice["familyId"] == family["id"]]
            if source["healthy"] and not baseline:
                for event in events:
                    if event["kind"] == "finished" and (
                        family["state"] != "finished" or source["armed"].get(family["id"]) != _membership(family)
                    ):
                        continue
                    if (event["kind"] == "waiting" and family["state"] != "waiting") or (
                        event["kind"] == "error" and not any(row["state"] == "error" for row in family["members"])
                    ):
                        continue
                    key = event["key"] if self.pairings[source["id"]].get("legacy") else digest(f"{source['id']}:{event['key']}")
                    notices.append({
                        "key": key, "kind": event["kind"], "title": f"{source['label']}: {family['title']}",
                        "message": (
                            "All observed family runs finished; not task or PR success."
                            if event["kind"] == "finished"
                            else "Family needs input or approval; the run is not finished."
                            if event["kind"] == "waiting"
                            else "Family has an error or interruption; not successful completion."
                            if event["kind"] == "error"
                            else "Family status is unconfirmed; no completion inferred."
                        ),
                    })
            if not invalid and family["state"] == "working":
                source["armed"][family["id"]] = _membership(family)
            elif family["state"] == "finished":
                source["armed"].pop(family["id"], None)

        try:
            await self.save()
        except Exception:
            self.sources[source["id"]] = before
            self.dismissed = dismissed_before
            raise

        for notice in notices:
            key = notice["key"]
            alert = {k: v for k, v in notice.items() if k != "key"}
            await self.notify(key, alert)
        return {"seq": source["seq"], "healthy": source["healthy"]}

    def disconnect(self, id_: str, lease: str) -> dict[str, Any]:
        source = self.sources.get(id_)
        if not source or source.get("lease") != lease:
            raise fail("Invalid watcher lease", 409)
        source["lease"] = None
        source["healthy"] = False
        source["armed"].clear()
        source["activeRuns"].clear()
        source["recoveryVotes"].clear()
        source["issues"] = ["Watcher stopped or disconnected"]
        return {"disconnected": True}

    def snapshot(self, now: float | None = None, include_hidden: bool = False) -> dict[str, Any]:
        if now is None:
            now = time.time() * 1000
        sessions: list[dict[str, Any]] = []
        members: list[dict[str, Any]] = []
        sources: list[dict[str, Any]] = []
        for pairing in self.pairings.values():
            if pairing["id"] not in self.sources:
                sources.append({
                    "id": pairing["id"], "label": pairing["label"], "healthy": False,
                    "lastSeen": None, "issues": ["Paired; watcher has not connected"],
                })
        for source in self.sources.values():
            online = self.live(source, now)
            if not online:
                source["armed"].clear()
                source["activeRuns"].clear()
                source["recoveryVotes"].clear()
            if source["id"] not in self.pairings:
                reason = "Reporter pairing revoked"
            elif not source["healthy"]:
                reason = "; ".join(source["issues"]) or "Watcher unavailable"
            else:
                reason = "Watcher heartbeat expired; source status is unconfirmed"
            rows = source["members"] if online else [_unknown(row, reason) for row in source["members"]]
            sources.append({
                "id": source["id"], "label": source["label"], "healthy": online,
                "lastSeen": _iso(source["lastSeen"]) if source.get("lastSeen") else None,
                "issues": source["issues"] if online else [reason],
            })
            members.extend(_mapped(row, source) for row in rows)
            for family in group_families(rows, source["relatives"]):
                row = _mapped(family, source)
                if include_hidden or not row.get("dismissKey") or self.dismissed.get(row["id"]) != row["dismissKey"]:
                    sessions.append(row)
        ordered = sort_sessions(sessions)
        return {
            "sessions": ordered,
            "members": sort_sessions(members),
            "sources": sources,
            "active": [row for row in ordered if row["state"] == "working"],
            "attention": [row for row in ordered if row["state"] in ("waiting", "error", "unknown")],
            "issues": [
                f"{source['label']} ({source['id'][:8]}): {issue}"
                for source in sources for issue in source["issues"]
            ],
            "coverage": f"{sum(1 for source in sources if source['healthy'])}/{len(sources)} paired Windows watchers connected",
        }

    def dismiss(self, entries: list[Any], now: float | None = None) -> dict[str, list[Any]]:
        if now is None:
            now = time.time() * 1000
        if not isinstance(entries, list) or not entries or len(entries) > 1000 or any(
            _invalid_dismiss_entry(entry) for entry in entries
        ):
            raise TypeError("Expected 1-1000 finished family IDs and revision keys")
        current = {row["id"]: row for row in self.snapshot(now, True)["sessions"]}
        result: dict[str, list[Any]] = {"dismissed": [], "skipped": []}
        for entry in entries:
            row = current.get(entry["id"])
            if not row or not row.get("dismissKey") or row["dismissKey"] != entry["key"]:
                result["skipped"].append({"id": entry["id"], "reason": "No longer the same connected, safely finished family"})
            else:
                self.dismissed[entry["id"]] = entry["key"]
                result["dismissed"].append(entry["id"])
        return result
