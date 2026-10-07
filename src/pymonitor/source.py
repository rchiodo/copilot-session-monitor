"""Port of src/source.mjs.

Ownership detection (inuse.<pid>.lock matching against a Windows process
snapshot), the raw-SQLite desktop-app hierarchy read, and per-session
JSONL activity tailing are ported unchanged -- these have no SDK
equivalent (no parent/child linkage, no live busy/idle field, no
per-turn granularity in SessionMetadata).

SDK integration point (see docs/porting-notes.md "SDK discovery"): the
CLI-only id discovery in `_discover_cli_session_ids()` below is backed by
github-copilot-sdk's `CopilotClient.list_sessions()` rather than hand-listing
~/.copilot/session-state/*/. Busy/idle detection still comes from JsonlTail,
unchanged -- SessionMetadata has no live status field. A single CopilotClient
is lazily started and reused for the life of a LocalSource instance (spawning
the bundled CLI process per poll would be far too heavy). `sdk_discover` is
an injectable async override so tests can supply canned ids without spawning
a real CLI process.
"""
from __future__ import annotations

import errno
import ntpath
import os
import re
import time
from typing import Any, Awaitable, Callable

from .events import JsonlTail

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a hard dependency at runtime
    psutil = None  # type: ignore[assignment]

from .hierarchy import hierarchy_index, related_metadata, selected_hierarchy

SESSION_ID = re.compile(r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$", re.IGNORECASE)

ProcessSnapshot = Callable[[], dict[str, Any] | None]


def _parse_date(value: str | None) -> float:
    if not value:
        return float("-inf")
    from datetime import datetime

    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000
    except ValueError:
        return float("-inf")


def _error_label(error: BaseException) -> str:
    code = getattr(error, "errno", None)
    if code is not None and code in errno.errorcode:
        return errno.errorcode[code]
    return type(error).__name__


def _pid_alive(pid: int) -> bool:
    if psutil is not None:
        return psutil.pid_exists(pid)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _activity(events: dict[str, Any], owner: dict[str, Any]) -> dict[str, Any]:
    def current(at: str | None) -> bool:
        return bool(at) and _parse_date(at) >= _parse_date(owner["startedAt"])

    background = (events.get("backgroundCount") or 0) > 0
    foreground = bool(events.get("activeTurn") or (events.get("activeTools") or 0) > 0) and not events.get("terminal")
    busy = (not events.get("closed")) and (
        (background and current(events.get("backgroundAt"))) or (foreground and current(events.get("lastExecutionAt")))
    )
    activity_unconfirmed = (
        "Outstanding background work lacks confirmed current ownership or lifecycle evidence"
        if events.get("backgroundUnconfirmed") or (background and not current(events.get("backgroundAt")))
        else None
    )
    completion_unconfirmed = (
        "Completion evidence predates the current process owner"
        if events.get("terminal") and not current(events["terminal"].get("at"))
        else None
    )
    return {"busy": busy, "activityUnconfirmed": activity_unconfirmed, "completionUnconfirmed": completion_unconfirmed}


def desktop_rows(file: str) -> list[dict[str, Any]]:
    import sqlite3

    conn = sqlite3.connect(f"file:{file}?mode=ro", uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        # Read only named metadata columns; never fetch credentials or transcripts.
        conn.execute("BEGIN")
        sessions = [dict(row) for row in conn.execute(
            """
            SELECT s.id, s.title, s.is_running, s.was_interrupted,
              s.execution_location, s.session_type, s.archived_at,
              (SELECT w.host_id FROM workspaces w WHERE w.session_id = s.id LIMIT 1) AS host_id
            FROM sessions s
            """
        )]
        workspaces = [dict(row) for row in conn.execute(
            "SELECT id,session_id,creator_session_id,host_id,archived_at FROM workspaces"
        )]
        links = [dict(row) for row in conn.execute(
            "SELECT child_workspace_id,parent_workspace_id FROM workspace_parent_links"
        )]
        aliases = [dict(row) for row in conn.execute(
            "SELECT session_id,workspace_id FROM workspace_session_aliases"
        )]
        workspace_chats = [dict(row) for row in conn.execute(
            "SELECT workspace_id,session_id FROM workspace_side_chats"
        )]
        session_chats = [dict(row) for row in conn.execute(
            "SELECT parent_session_id,session_id FROM session_side_chats"
        )]
        nodes = hierarchy_index(
            sessions=sessions,
            workspaces=workspaces,
            links=links,
            aliases=aliases,
            workspace_chats=workspace_chats,
            session_chats=session_chats,
        )
        conn.execute("COMMIT")
        return list(nodes.values())
    finally:
        conn.close()


_LOCK_RE = re.compile(r"^inuse\.(\d+)\.lock$")


SdkDiscover = Callable[[], Awaitable[list[str]]]


class LocalSource:
    def __init__(
        self,
        home: str,
        process_snapshot: ProcessSnapshot,
        *,
        sdk_discover: SdkDiscover | None = None,
    ) -> None:
        self.home = home
        self.root = os.path.join(home, "session-state")
        self.process_snapshot = process_snapshot
        self.tails: dict[str, JsonlTail] = {}
        self.live_ids: set[str] = set()
        self.tracked: set[str] = set()
        self.directory_ids: list[str] = []
        self.discovered_at = 0.0
        # Test-only override; production callers rely on the real SDK-backed
        # default (_default_sdk_discover), lazily started on first use.
        self._sdk_discover_override = sdk_discover
        self._sdk_client: Any | None = None
        # Consecutive poll() failures, tracked here (rather than per-call
        # state in local_report.py) because both server.py and watcher.py
        # may rebuild their ctx dict on every poll -- this object is the one
        # thing that's guaranteed to persist across polls in both call sites.
        # See poll_local()'s transient-failure grace window.
        self.consecutive_failures = 0

    async def owner(self, id_: str, processes: list[dict[str, Any]], desktop: bool) -> dict[str, Any] | None:
        dir_ = os.path.join(self.root, id_)
        entries = os.listdir(dir_)
        matches = []
        for name in entries:
            match = _LOCK_RE.match(name)
            if not match:
                continue
            pid = int(match.group(1))
            process_info = next((p for p in processes if p["pid"] == pid and p["name"] == "copilot.exe"), None)
            if not process_info:
                continue
            if not _pid_alive(process_info["pid"]):
                continue
            lock_mtime_ms = os.stat(os.path.join(dir_, name)).st_mtime * 1000
            if _parse_date(process_info["startedAt"]) > lock_mtime_ms + 2000:
                continue
            parent = next(
                (p for p in processes if p["pid"] == process_info.get("parentPid") and p["name"] == "github.exe"),
                None,
            )
            if desktop and (not parent or _parse_date(parent["startedAt"]) > _parse_date(process_info["startedAt"])):
                continue
            if desktop and not _pid_alive(parent["pid"]):
                continue
            if not desktop and parent:
                continue
            matches.append({
                "key": f"{process_info['pid']}:{process_info['startedAt']}:{parent['startedAt'] if parent else 'cli'}",
                "startedAt": process_info["startedAt"],
            })
        if len(matches) > 1:
            raise RuntimeError("Multiple live session owners")
        return matches[0] if matches else None

    async def poll(self) -> dict[str, Any]:
        process_state = self.process_snapshot()
        now_ms = time.time() * 1000
        if not process_state or now_ms - process_state["at"] > 8000 or process_state.get("error"):
            raise RuntimeError("Windows process observer unavailable")

        rows = desktop_rows(os.path.join(self.home, "data.db"))
        known_ids = {row["id"] for row in rows}
        nodes = {row["id"]: row for row in rows}
        samples: list[dict[str, Any]] = []
        issues: list[str] = []
        reads: dict[str, dict[str, Any]] = {}
        discovered: set[str] = set(self.tracked)
        self.live_ids.clear()

        async def read(row: dict[str, Any]) -> dict[str, Any]:
            owner = await self.owner(row["id"], process_state["processes"], True)
            if not owner:
                return {"owner": None, "events": None}
            self.live_ids.add(row["id"])
            tail = self.tails.setdefault(row["id"], JsonlTail(os.path.join(self.root, row["id"], "events.jsonl")))
            events = await tail.read()
            return {"owner": owner, "events": events, **_activity(events, owner)}

        # A foreground-idle session can still own an attached command or background agent.
        # Probe live local owners, but retain only actual work/uncertainty, not idle history.
        for row in rows:
            if (
                row["id"] in self.tracked
                or row.get("is_running")
                or row.get("archived_at")
                or row.get("execution_location") != "local"
                or (row.get("host_id") and row.get("host_id") != "local")
                or not SESSION_ID.match(row["id"])
            ):
                continue
            try:
                result = await read(row)
                reads[row["id"]] = result
                if result.get("busy") or result.get("activityUnconfirmed"):
                    discovered.add(row["id"])
            except OSError as error:
                if getattr(error, "errno", None) != errno.ENOENT or row["id"] in self.live_ids:
                    label = _error_label(error)
                    issues.append(f"{row['title']}: activity discovery unavailable ({label})")
                    reads[row["id"]] = {"readError": f"Activity discovery unavailable ({label})"}
                    if row["id"] in self.live_ids:
                        discovered.add(row["id"])

        local = selected_hierarchy(nodes, discovered)
        for row in local:
            title = row.get("title") or f"Name unavailable ({row['id'][:8]})"
            sample: dict[str, Any] = {
                "id": row["id"],
                "title": title,
                "source": "Copilot desktop",
                "busy": row.get("is_running") == 1,
                "interrupted": row.get("was_interrupted") == 1,
                "alive": False,
                "owner": None,
                "events": None,
                "parentId": row.get("parentId"),
                "hierarchyIssue": row.get("hierarchyIssue"),
                "contextOnly": row["id"] not in discovered and not row.get("is_running"),
            }
            if not SESSION_ID.match(row["id"]) or not row.get("local") or row.get("archived_at"):
                sample["readError"] = row.get("hierarchyIssue") or (
                    "Related app session is archived; current status unavailable"
                    if row.get("archived_at")
                    else "Related session is outside local coverage"
                )
                sample["hierarchyIssue"] = sample["readError"]
                sample["contextOnly"] = True
                issues.append(f"{sample['title']}: {sample['readError']}")
                samples.append(sample)
                continue
            if row.get("hierarchyIssue"):
                issues.append(f"{sample['title']}: {row['hierarchyIssue']}")
            try:
                cached = reads.get(row["id"])
                result = cached if cached is not None else await read(row)
                owner = result.get("owner")
                sample["readError"] = result.get("readError")
                if owner:
                    sample["alive"] = True
                    sample["owner"] = owner["key"]
                    sample["events"] = result["events"]
                    sample["busy"] = bool(sample["busy"]) or bool(result.get("busy"))
                    sample["activityUnconfirmed"] = result.get("activityUnconfirmed")
                    sample["completionUnconfirmed"] = result.get("completionUnconfirmed")
                    if sample["busy"] and (not sample["events"].get("runId") or sample["events"].get("closed")):
                        issues.append(
                            f"{sample['title']}: running flag lacks current execution evidence; not counted as working"
                        )
                    if sample["busy"] or sample.get("activityUnconfirmed"):
                        self.tracked.add(row["id"])
                elif sample["busy"]:
                    issues.append(f"{sample['title']}: running flag has no live local owner; not counted as working")
            except OSError as error:
                sample["readError"] = f"Session reader unavailable ({_error_label(error)})"
                issues.append(f"{sample['title']}: {sample['readError']}")
            samples.append(sample)

        if now_ms - self.discovered_at > 5000:
            try:
                self.directory_ids = await self._discover_cli_session_ids()
            except Exception as error:  # noqa: BLE001 - SDK/subprocess failures must not crash a poll cycle
                issues.append(f"CLI session discovery unavailable ({_error_label(error)})")
            finally:
                self.discovered_at = now_ms

        for id_ in self.directory_ids:
            if id_ in known_ids:
                continue
            try:
                owner = await self.owner(id_, process_state["processes"], False)
                if not owner:
                    continue
                tail = self.tails.setdefault(id_, JsonlTail(os.path.join(self.root, id_, "events.jsonl")))
                events = await tail.read()
                cwd = events.get("cwd")
                title = f"{ntpath.basename(cwd)} - CLI {id_[:8]}" if cwd else f"CLI {id_[:8]}"
                samples.append({
                    "id": id_,
                    "title": title,
                    "source": "CLI (activity only)",
                    "busy": events.get("activeTurn"),
                    "interrupted": False,
                    "alive": True,
                    "owner": owner["key"],
                    "events": events,
                })
            except OSError as error:
                if getattr(error, "errno", None) != errno.ENOENT:
                    issues.append(f"CLI {id_[:8]}: reader unavailable ({_error_label(error)})")

        return {
            "samples": samples,
            "issues": issues,
            "relatives": related_metadata(nodes, local),
            "desktopSessions": len(
                [row for row in rows if row.get("execution_location") == "local" and not row.get("archived_at")]
            ),
        }

    async def _discover_cli_session_ids(self) -> list[str]:
        """Which Copilot CLI session ids exist, via the official SDK.

        This is the one architectural change requested for this port: no
        more hand-listing ~/.copilot/session-state/*/. `sdk_discover`
        (constructor arg) lets tests supply canned ids without spawning a
        real CLI process; production code falls through to
        `_default_sdk_discover`.
        """
        discover = self._sdk_discover_override or self._default_sdk_discover
        ids = await discover()
        return [id_ for id_ in ids if SESSION_ID.match(id_)]

    async def _default_sdk_discover(self) -> list[str]:
        import copilot  # deferred import: optional at module import time, like psutil above

        client = self._sdk_client
        if client is None:
            client = copilot.CopilotClient(base_directory=self.home)
            await client.start()
            self._sdk_client = client
        sessions = await client.list_sessions()
        return [session.session_id for session in sessions]

    async def aclose(self) -> None:
        """Stop the lazily-started SDK client, if one was created."""
        if self._sdk_client is not None:
            await self._sdk_client.stop()
            self._sdk_client = None

    def release_idle(self, keep_ids: set[str]) -> None:
        for id_ in list(self.tracked):
            if id_ not in keep_ids:
                self.tracked.discard(id_)
                self.tails.pop(id_, None)
        for id_ in list(self.tails.keys()):
            if id_ not in keep_ids and id_ not in self.tracked and id_ not in self.live_ids:
                self.tails.pop(id_, None)
