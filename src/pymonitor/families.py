"""Port of src/families.mjs.

Groups individual session rows into "families" (one root + descendants),
computes family-level state/detail/dismissKey, and wraps MonitorEngine
with family-level alerting + revision-key-safe dismissal.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable

from .engine import MonitorEngine, sort_sessions
from .hierarchy import root_of

_KEY_RE = re.compile(r"^[a-f0-9]{64}$")


def _coalesce(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _sha256_json(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode("utf-8")).hexdigest()


def family_details(root_id: str, members: Iterable[dict[str, Any]], relatives: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    members = list(members)
    nodes: dict[str, dict[str, Any]] = {row["id"]: {**row, "state": "unobserved"} for row in relatives}
    for row in members:
        nodes[row["id"]] = {**nodes.get(row["id"], {}), **row}

    selected = [row for row in nodes.values() if root_of(row["id"], nodes)["id"] == root_id]
    ordered: list[dict[str, Any]] = []
    visited: set[str] = set()

    def walk(id_: str, depth: int) -> None:
        if id_ in visited:
            return
        visited.add(id_)
        row = nodes.get(id_)
        if row:
            ordered.append({**row, "depth": depth})
        children = sorted(
            (r for r in selected if r.get("parentId") == id_),
            key=lambda r: r.get("title") or "",
        )
        for child in children:
            walk(child["id"], depth + 1)

    walk(root_id, 0)
    for row in members:
        if row["id"] not in visited:
            ordered.append({**row, "depth": 1})
    return ordered


def group_families(rows: Iterable[dict[str, Any]], relatives: Iterable[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    relatives = list(relatives or [])
    rows = list(rows)
    nodes: dict[str, dict[str, Any]] = {row["id"]: row for row in rows}
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        root = root_of(row["id"], nodes)
        group = groups.setdefault(root["id"], {"rows": [], "issue": root["issue"]})
        group["rows"].append(row)
        if group["issue"] is None:
            group["issue"] = _coalesce(row.get("hierarchyIssue"), root["issue"])

    out = []
    for id_, group in groups.items():
        parent = nodes.get(id_) or {
            **group["rows"][0],
            "id": id_,
            "title": f"Unavailable parent {id_[:8]}",
            "state": "unknown",
            "lastResponseAt": None,
            "lastAlert": None,
            "finishedAt": None,
        }
        working = [row for row in group["rows"] if row.get("state") == "working"]
        observed = [row for row in group["rows"] if not row.get("contextOnly")]
        unknown = any(row.get("state") == "unknown" for row in group["rows"]) or bool(group["issue"])
        if working:
            state = "working"
        elif unknown:
            state = "unknown"
        elif any(row.get("state") == "error" for row in group["rows"]):
            state = "error"
        elif any(row.get("state") == "waiting" for row in group["rows"]):
            state = "waiting"
        elif observed and all(row.get("state") == "finished" for row in observed):
            state = "finished"
        else:
            state = "unknown"

        finished_at = None
        if state == "finished":
            times = sorted(row["finishedAt"] for row in observed if row.get("finishedAt"))
            finished_at = times[-1] if times else None

        dismiss_key = None
        if (state == "finished" or state == "unknown") and not group["issue"]:
            canonical = [
                state,
                sorted(
                    [
                        row["id"],
                        row.get("parentId"),
                        row.get("runId"),
                        row.get("startedAt"),
                        row.get("finishedAt"),
                        bool(row.get("contextOnly")),
                    ]
                    for row in group["rows"]
                ),
            ]
            dismiss_key = _sha256_json(canonical)

        if state == "working":
            detail = f"{len(working)} observed family member(s) working"
        elif state == "finished":
            detail = "All observed family runs finished; not task or PR success"
        else:
            detail = _coalesce(
                group["issue"],
                "Family completion unconfirmed; inspect member status" if state == "unknown"
                else "Family needs input or approval" if state == "waiting"
                else "Family has an error or interruption",
            )

        out.append({
            **parent,
            "state": state,
            "finishedAt": finished_at,
            "hierarchyIssue": group["issue"],
            "detail": detail,
            "parentAlert": _coalesce(parent.get("lastAlert"), None),
            "parentState": parent.get("state"),
            "runningCount": len(working),
            "childCount": len([row for row in group["rows"] if row["id"] != id_]),
            "members": sort_sessions(group["rows"]),
            "relatives": family_details(id_, group["rows"], relatives),
            "dismissKey": dismiss_key,
        })
    return sort_sessions(out)


class FamilyMonitor:
    def __init__(
        self,
        machine: str,
        emit: Callable[[str, dict[str, Any]], Awaitable[None]],
        retained: Iterable[dict[str, Any]] = (),
        dismissed: dict[str, str] | None = None,
    ) -> None:
        self.emit = emit
        self.pending: list[dict[str, Any]] = []

        async def _record(key: str, alert: dict[str, Any]) -> None:
            self.pending.append({"key": key, **alert})

        self.engine = MonitorEngine(machine, _record, list(retained))
        self.armed: dict[str, str] = {}
        self.dismissed: dict[str, str] = dict(dismissed or {})
        self.relatives: list[dict[str, Any]] = []
        self.startup_hidden: set[str] = {
            row["id"]
            for row in group_families(list(retained))
            if row.get("dismissKey") and self.dismissed.get(row["id"]) == row["dismissKey"]
        }

    @property
    def rows(self) -> dict[str, dict[str, Any]]:
        return self.engine.rows

    @property
    def observed(self) -> dict[str, dict[str, Any]]:
        return self.engine.observed

    def snapshot(self, gap: bool = False) -> dict[str, Any]:
        members = sort_sessions(list(self.rows.values()))
        sessions = [
            row
            for row in group_families(members, self.relatives)
            if row["id"] not in self.startup_hidden
            and (not row.get("dismissKey") or self.dismissed.get(row["id"]) != row["dismissKey"])
        ]
        return {
            "members": members,
            "sessions": sessions,
            "active": [row for row in sessions if row["state"] == "working"],
            "attention": [row for row in sessions if row["state"] in ("waiting", "error", "unknown")],
            "gap": gap,
        }

    def dismiss(self, entries: list[dict[str, Any]]) -> dict[str, list[Any]]:
        if (
            not isinstance(entries, list)
            or not entries
            or len(entries) > 1000
            or any(
                not isinstance(entry.get("id"), str)
                or len(entry["id"]) > 200
                or not isinstance(entry.get("key"), str)
                or not _KEY_RE.match(entry["key"])
                for entry in entries
            )
        ):
            raise TypeError("Expected 1-1000 dismissable family IDs and revision keys")

        current = {row["id"]: row for row in group_families(list(self.rows.values()), self.relatives)}
        result: dict[str, list[Any]] = {"dismissed": [], "skipped": []}
        deduped = {entry["id"]: entry for entry in entries}
        for entry in deduped.values():
            row = current.get(entry["id"])
            # entry["key"] is still required and format-validated above, but is no
            # longer compared against the row's current dismissKey. Requiring an
            # exact match made "Clear retained" unreliable: any poll tick landing
            # between the dashboard's last render and the user's click -- even one
            # that represents no meaningful change -- changes the hash and silently
            # dropped the entry into "skipped" with no obvious way to retry. The
            # only protection that matters is still enforced: a family must
            # currently be in a dismissable state (finished/unknown -> dismissKey
            # truthy); working/waiting/error rows have no dismissKey and stay
            # un-dismissable.
            if not row or not row.get("dismissKey"):
                result["skipped"].append({"id": entry["id"], "reason": "No longer a dismissable finished family"})
            else:
                self.dismissed[entry["id"]] = row["dismissKey"]
                result["dismissed"].append(entry["id"])
        return result

    async def update(self, samples: Iterable[dict[str, Any]], options: dict[str, Any] | None = None) -> dict[str, Any]:
        options = options or {}
        self.pending = []
        was_baseline = self.engine.baseline
        update_kwargs: dict[str, Any] = {}
        if "healthy" in options:
            update_kwargs["healthy"] = options["healthy"]
        if "now" in options:
            update_kwargs["now"] = options["now"]
        if "reason" in options:
            update_kwargs["reason"] = options["reason"]
        result = await self.engine.update(samples, **update_kwargs)
        self.startup_hidden.clear()
        if options.get("relatives") is not None:
            self.relatives = options["relatives"]
        now_ms = options.get("now")
        at = datetime.fromtimestamp((now_ms if now_ms is not None else datetime.now(timezone.utc).timestamp() * 1000) / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")

        changed = []
        for event in self.pending:
            row = self.rows.get(event["sessionId"])
            if not row or (row.get("lastAlert") or {}).get("key") == event["key"]:
                continue
            last_alert = {"sessionId": row["id"], "key": event["key"], "kind": event["kind"], "message": event["message"], "at": at}
            self.rows[row["id"]] = {**row, "lastAlert": last_alert}
            changed.append(event)

        families = group_families(list(self.rows.values()), self.relatives)
        interrupted = options.get("healthy") is False or result.get("gap")
        if interrupted:
            self.armed.clear()

        for family in families:
            if (family["state"] in ("working", "waiting", "error")) or (
                family.get("dismissKey") and self.dismissed.get(family["id"]) != family["dismissKey"]
            ):
                self.dismissed.pop(family["id"], None)

            membership = "|".join(sorted(row["id"] for row in family["members"]))
            invalid = bool(interrupted) or bool(family.get("hierarchyIssue")) or any(
                row["state"] == "unknown" and row["id"] not in self.observed for row in family["members"]
            )
            if invalid:
                self.armed.pop(family["id"], None)
            elif family["state"] == "working":
                self.armed[family["id"]] = membership
            elif self.armed.get(family["id"]) != membership:
                self.armed.pop(family["id"], None)

            events = [event for event in changed if any(row["id"] == event["sessionId"] for row in family["members"])]
            notices = [event for event in events if event["kind"] != "finished"]
            if notices and (not was_baseline or interrupted):
                kind = next((k for k in ("error", "waiting", "warning") if any(e["kind"] == k for e in notices)), None)
                key = f"family:{family['id']}:{'|'.join(sorted(e['key'] for e in notices))}"
                trigger = next(e for e in notices if e["kind"] == kind)
                await self.emit(key, {
                    "kind": kind,
                    "familyId": family["id"],
                    "title": family["title"],
                    "message": f"{len(notices)} family member alert(s). {trigger['message']}",
                })

            if not invalid and family["state"] == "finished" and family["id"] in self.armed and not was_baseline:
                runs = sorted(
                    [row["id"], row.get("runId")] for row in family["members"] if not row.get("contextOnly")
                )
                key = _sha256_json(runs)
                await self.emit(f"family:{family['id']}:{key}:finished", {
                    "kind": "finished",
                    "familyId": family["id"],
                    "title": family["title"],
                    "message": "All observed family runs finished. This does not mean the entire task or PR succeeded.",
                })
                self.armed.pop(family["id"], None)

        ids = {row["id"] for row in families}
        for id_ in list(self.armed.keys()):
            if id_ not in ids:
                del self.armed[id_]
        for id_ in list(self.dismissed.keys()):
            if id_ not in ids:
                del self.dismissed[id_]

        return self.snapshot(result.get("gap"))
