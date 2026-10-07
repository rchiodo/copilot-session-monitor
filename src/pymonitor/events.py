"""Port of src/events.mjs.

EventState is the real status engine: it derives fine-grained per-turn/
per-tool/per-gate status ("Input needed", "Plan approval needed",
"Executing tools", "Background work running", "Agent running") by walking
a session's events.jsonl. This is intentionally NOT replaced by the
Copilot SDK's on_lifecycle, which only exposes coarse session create/
delete/foreground/background events with no per-turn/per-tool granularity
-- see docs/porting-notes.md ("SDK integration boundary").

JsonlTail is the rotation-safe tail reader: it re-validates file identity
(dev/ino/birthtime) and an anchor/prefix byte-range on every read so a
truncated, replaced, or rotated events.jsonl is detected and never silently
misread as a continuation of the previous file.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from pymonitor.background import BackgroundWork

MAX_LINE = 32 * 1024 * 1024

_GATE_TYPES = {
    "permission": "Permission needed",
    "user_input": "Input needed",
    "exit_plan_mode": "Plan approval needed",
    "elicitation": "Input needed",
}
_INPUT_TOOL = re.compile(r"^(?:(?:functions|[^.]+)\.)?(ask_user|askUser|exit_plan_mode)$")


def _parse_date(value: str | None) -> float:
    """Returns a comparable epoch-ms float, or -inf for unparsable/None (mirrors Date.parse -> NaN handling)."""
    if not value:
        return float("-inf")
    try:
        text = value.replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp() * 1000
    except ValueError:
        return float("-inf")


def _is_finite_date(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return _parse_date(value) != float("-inf")


@dataclass
class _Gate:
    id: str
    kind: str
    toolCallId: str | None = None


class EventState:
    """Mirrors events.mjs's EventState class exactly."""

    def __init__(self) -> None:
        self.run_id: str | None = None
        self.started_at: str | None = None
        self.interaction_id: str | None = None
        self.turn_id: str | None = None
        self.active_turn = False
        self.closed = False
        self.error: dict[str, Any] | None = None
        self.terminal: dict[str, Any] | None = None
        self.last_event_at: str | None = None
        self.last_response_at: str | None = None
        self.last_execution_id: str | None = None
        self.tools: set[str] = set()
        self.gates: dict[str, _Gate] = {}
        self.final_message = False
        self.final_turn_end: dict[str, Any] | None = None
        self.background = BackgroundWork()
        self.last_execution_at: str | None = None
        self.cwd: str | None = None
        self.seen: set[str] = set()

    def accept(self, event: dict[str, Any]) -> None:
        if not isinstance(event.get("id"), str) or not isinstance(event.get("type"), str) or not _is_finite_date(event.get("timestamp")):
            raise ValueError("Unsupported event envelope")
        if event["id"] in self.seen:
            return
        self.seen.add(event["id"])
        if len(self.seen) > 4096:
            self.seen.discard(next(iter(self.seen)))
        d = event.get("data") or {}
        self.background.accept(event)
        # Subagents have their own turns; they cannot finish or start the root run.
        if event.get("agentId") or d.get("parentToolCallId"):
            self._settle_background(event)
            return
        event_type = event["type"]
        self.last_event_at = event["timestamp"]
        if event_type in ("session.start", "session.resume"):
            context = d.get("context") or {}
            if isinstance(context.get("cwd"), str):
                self.cwd = context["cwd"]
            self.run_id = None
            self.active_turn = False
            self.closed = False
            self.error = None
            self.terminal = None
            self.final_turn_end = None
            self.tools.clear()
            self.gates.clear()
        elif event_type == "assistant.turn_start":
            new_run = (
                not self.run_id
                or self.terminal is not None
                or self.closed
                or (d.get("interactionId") and d["interactionId"] != self.interaction_id)
            )
            if new_run:
                self.run_id = event["id"]
                self.started_at = event["timestamp"]
                self.tools.clear()
            self.interaction_id = d.get("interactionId", self.interaction_id)
            self.turn_id = d.get("turnId")
            self.active_turn = True
            self.closed = False
            self.error = None
            self.terminal = None
            self.final_message = False
            self.final_turn_end = None
            self.gates.clear()
            self.last_execution_id = event["id"]
            self.last_execution_at = event["timestamp"]
        elif event_type == "assistant.message":
            if not self.last_response_at or _parse_date(event["timestamp"]) > _parse_date(self.last_response_at):
                self.last_response_at = event["timestamp"]
            if d.get("turnId") == self.turn_id:
                tool_requests = d.get("toolRequests")
                phase = d.get("phase")
                chunk_count = d.get("chunkCount")
                self.final_message = (
                    isinstance(tool_requests, list)
                    and len(tool_requests) == 0
                    and (phase is None or phase in ("final_answer", "final"))
                    and (chunk_count is None or d.get("chunkIndex") == chunk_count - 1)
                )
        elif event_type == "assistant.turn_end" and d.get("turnId") == self.turn_id:
            self.active_turn = False
            if self.final_message and not self.tools and not self.gates and not self.error:
                self.final_turn_end = {"id": event["id"], "at": event["timestamp"]}
                self._settle_background(event)
        elif event_type == "tool.execution_start":
            self.tools.add(d["toolCallId"])
            self.last_execution_id = event["id"]
            self.last_execution_at = event["timestamp"]
            self.terminal = None
            tool_name = d.get("toolName") or ""
            if _INPUT_TOOL.match(tool_name):
                self.gates[f"tool:{d['toolCallId']}"] = _Gate(
                    id=event["id"],
                    kind="Plan approval needed" if re.search("plan", tool_name) else "Input needed",
                    toolCallId=d["toolCallId"],
                )
        elif event_type == "tool.execution_complete":
            self.tools.discard(d.get("toolCallId"))
            for key, gate in list(self.gates.items()):
                if gate.toolCallId == d.get("toolCallId"):
                    del self.gates[key]
        elif event_type == "external_tool.requested" and _INPUT_TOOL.match(d.get("toolName") or ""):
            tool_name = d.get("toolName") or ""
            self.gates[f"external:{d['requestId']}"] = _Gate(
                id=event["id"],
                kind="Plan approval needed" if re.search("plan", tool_name) else "Input needed",
                toolCallId=d.get("toolCallId"),
            )
        elif event_type == "external_tool.completed":
            self.gates.pop(f"external:{d.get('requestId')}", None)
        elif event_type == "session.task_complete":
            if d.get("success") is True and not self.error and not self.gates:
                self.final_turn_end = {"id": event["id"], "at": event["timestamp"]}
                self._settle_background(event)
            elif d.get("success") is False:
                self.error = {"id": event["id"], "kind": "Run ended with an unsuccessful result"}
                self.terminal = None
        elif event_type in ("abort", "session.error"):
            self.error = {
                "id": event["id"],
                "kind": "Run interrupted" if event_type == "abort" else "Session reported an error",
            }
            self.terminal = None
            self.active_turn = False
        elif event_type == "session.shutdown":
            self.closed = True
            self.active_turn = False
        else:
            prefix, _, action = event_type.partition(".")
            if prefix in _GATE_TYPES and action == "requested" and not d.get("resolvedByHook"):
                permission_request = d.get("permissionRequest") or {}
                self.gates[f"{prefix}:{d.get('requestId')}"] = _Gate(
                    id=event["id"],
                    kind=_GATE_TYPES[prefix],
                    toolCallId=d.get("toolCallId") or permission_request.get("toolCallId"),
                )
            elif prefix in _GATE_TYPES and action == "completed":
                self.gates.pop(f"{prefix}:{d.get('requestId')}", None)
        if event_type in ("system.notification", "tool.execution_complete"):
            self._settle_background(event)

    def _settle_background(self, event: dict[str, Any]) -> None:
        work = self.background.snapshot()
        # An acknowledged failure (the agent kept doing things after it, and the
        # turn ended on its own successful terms) no longer vetoes settling to
        # "finished" -- see BackgroundWork.failure_acknowledged.
        if work["backgroundCount"] or work["backgroundUnconfirmed"] or (work["backgroundFailure"] and not work["backgroundFailureAcknowledged"]):
            self.terminal = None
        elif self.final_turn_end and not self.tools and not self.gates and not self.error:
            if self.terminal is None:
                self.terminal = {"id": event["id"], "at": event["timestamp"]}

    def snapshot(self) -> dict[str, Any]:
        work = self.background.snapshot()
        gate = next(iter(self.gates.values()), None)
        return {
            **work,
            "runId": self.run_id,
            "startedAt": self.started_at,
            "activeTurn": self.active_turn,
            "closed": self.closed,
            "error": self.error if self.error else (
                work["backgroundFailure"]
                if not self.active_turn and self.final_turn_end and not work["backgroundFailureAcknowledged"]
                else None
            ),
            "terminal": None if (work["backgroundCount"] or work["backgroundUnconfirmed"]) else self.terminal,
            "lastEventAt": self.last_event_at,
            "lastExecutionId": self.last_execution_id,
            "lastExecutionAt": self.last_execution_at,
            "activeTools": len(self.tools),
            "lastResponseAt": self.last_response_at,
            "cwd": self.cwd,
            "waiting": {"id": gate.id, "kind": gate.kind, "toolCallId": gate.toolCallId} if gate else None,
            "activity": "Background work running" if work["backgroundCount"] else ("Executing tools" if self.tools else "Agent running"),
        }


def _bytes_at(handle, start: int, length: int) -> bytes:
    if length <= 0 or start < 0:
        return b""
    handle.seek(start)
    return handle.read(length)


class JsonlTail:
    """Mirrors events.mjs's JsonlTail class exactly."""

    def __init__(self, file: str) -> None:
        self.file = file
        self._reset()

    def _reset(self) -> None:
        self.offset = 0
        self.pending = b""
        self.identity: str | None = None
        self.anchor = b""
        self.prefix = b""
        self.state = EventState()

    async def read(self) -> dict[str, Any]:
        # Synchronous file IO is acceptable here: identical to the Node original's
        # use of fs/promises, which is also just a thin async wrapper over blocking
        # syscalls under the hood. Kept `async def` so callers (poll loops) stay
        # coroutine-friendly without requiring a thread pool.
        with open(self.file, "rb") as handle:
            try:
                st = os.fstat(handle.fileno())
                identity = f"{st.st_dev}:{st.st_ino}:{getattr(st, 'st_birthtime', st.st_ctime)}"
                replaced = False
                if self.identity:
                    anchor = _bytes_at(handle, self.offset - len(self.anchor), len(self.anchor))
                    prefix = _bytes_at(handle, 0, len(self.prefix))
                    replaced = (
                        identity != self.identity
                        or st.st_size < self.offset
                        or anchor != self.anchor
                        or prefix != self.prefix
                    )
                    if replaced:
                        self._reset()
                self.identity = identity
                initial = self.offset == 0
                while self.offset < st.st_size:
                    chunk = _bytes_at(handle, self.offset, min(256 * 1024, st.st_size - self.offset))
                    if not chunk:
                        raise OSError("Event file changed during read")
                    self.offset += len(chunk)
                    data = self.pending + chunk
                    start = 0
                    while True:
                        end = data.find(b"\n", start)
                        if end == -1:
                            break
                        if end - start > MAX_LINE:
                            raise ValueError("Event line exceeds supported size")
                        if end > start:
                            self.state.accept(json.loads(data[start:end].decode("utf-8")))
                        start = end + 1
                    self.pending = data[start:]
                    if len(self.pending) > MAX_LINE:
                        raise ValueError("Partial event exceeds supported size")
                self.anchor = _bytes_at(handle, max(0, self.offset - 256), min(256, self.offset))
                self.prefix = _bytes_at(handle, 0, min(256, self.offset))
                snapshot = self.state.snapshot()
                return {**snapshot, "replaced": replaced, "initial": initial, "partial": len(self.pending) > 0}
            except Exception:
                self._reset()
                raise
