"""Port of src/background.mjs.

Tracks background shell/agent work observed in a session's events.jsonl so
the status engine can tell the difference between "the model turn ended"
and "the run is actually done" (detached shells, subagents, etc. can keep
running after the root turn finishes). Faithful line-for-line port --
the original's regex-based shell-tool-output parsing is unusual but load
bearing (it decodes the Copilot runtime's own shell-result envelopes),
so it is kept exactly as-is rather than "cleaned up".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_SHELL_TOOL = re.compile(r"^(?:functions\.)?(powershell|read_powershell|stop_powershell)$")
_SHELL_ID = r"[A-Za-z0-9_.:-]+"


def _shell_result(data: dict[str, Any]) -> dict[str, Any] | None:
    result = data.get("result") or {}
    contents = result.get("contents")
    exit_item = None
    if isinstance(contents, list):
        exit_item = next((item for item in contents if item.get("type") == "shell_exit"), None)
    if exit_item is not None and isinstance(exit_item.get("exitCode"), int):
        return {"id": exit_item.get("shellId"), "exitCode": exit_item["exitCode"]}

    raw_content = result.get("content")
    text = raw_content.strip() if isinstance(raw_content, str) else ""

    # Match runtime envelopes, not arbitrary command output containing status-like text.
    ended = re.search(rf"(?:^|\n)<shellId: ({_SHELL_ID}) completed with exit code (-?\d+)>$", text)
    if ended:
        return {"id": ended.group(1), "exitCode": int(ended.group(2))}
    # A purely synchronous command that finishes within the tool call itself never gets
    # assigned a shellId at all -- the runtime just reports its exit code inline, with no
    # id to correlate against. Recognizing this keeps such calls from falling through to
    # the "unresolvable" branch below and leaving a permanent, uncorrelated "unknown" entry.
    bare_exit = re.search(r"(?:^|\n)<exited with exit code (-?\d+)>$", text)
    if bare_exit:
        return {"exitCode": int(bare_exit.group(1))}
    # The runtime labels a tracked shell "shellId" (auto-generated) when the caller let it
    # pick an id, or "sessionId" (caller-chosen, e.g. a persistent language-server session)
    # when the caller named one explicitly -- both identify the same kind of correlatable
    # background shell, so accept either label.
    running = re.search(
        rf"(?:^|\n)<command with (?:shellId|sessionId): ({_SHELL_ID}) is still running after \d+ seconds\.[^<>]*>$",
        text,
    )
    if running:
        return {"id": running.group(1), "running": True}
    moved = re.search(
        rf"(?:^|\n)<command with (?:shellId|sessionId): ({_SHELL_ID}) moved to background by the user\.[^<>]*>$",
        text,
    )
    if moved:
        return {"id": moved.group(1), "running": True}
    started = re.search(
        rf"^<command started in (detached )?background with (?:shellId|sessionId): ({_SHELL_ID})>$", text
    )
    if started:
        return {"id": started.group(2), "running": True, "detached": bool(started.group(1))}
    stopped = re.search(rf"^<command with id: ({_SHELL_ID}) stopped>$", text)
    if stopped:
        return {"id": stopped.group(1), "stopped": True}
    shell_execution = data.get("shellExecution") or {}
    if isinstance(shell_execution.get("exitCode"), int):
        return {"exitCode": shell_execution["exitCode"]}
    return None


@dataclass
class _Shell:
    detached: bool
    at: str
    state: str  # 'running' | 'unknown' | 'done'


class BackgroundWork:
    """Mirrors background.mjs's BackgroundWork class exactly."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.calls: dict[str, dict[str, Any]] = {}
        self.shells: dict[str, _Shell] = {}
        self.agents: dict[str, str] = {}
        self.failure: dict[str, Any] | None = None

    def accept(self, event: dict[str, Any]) -> None:
        d = event.get("data") or {}
        event_type = event["type"]
        agent_id = event.get("agentId")

        if event_type in ("session.start", "session.resume") and not agent_id:
            self.reset()

        if event_type == "subagent.started" and agent_id:
            self.agents[agent_id] = event["timestamp"]
        elif event_type in ("subagent.completed", "subagent.failed"):
            pending = agent_id in self.agents
            self.agents.pop(agent_id, None)
            if pending and (event_type == "subagent.failed" or d.get("cancelled")):
                self.failure = {
                    "id": event["id"],
                    "kind": "Background agent cancelled" if d.get("cancelled") else "Background agent failed",
                }
        elif event_type == "assistant.turn_start":
            if agent_id:
                self.agents[agent_id] = event["timestamp"]
            else:
                self.failure = None
        elif event_type == "system.notification":
            kind = d.get("kind") or {}
            kind_type = kind.get("type")
            if kind_type == "agent_completed" and kind.get("status") == "failed" and kind.get("agentId") in self.agents:
                self.failure = {"id": event["id"], "kind": "Background agent failed"}
            if kind_type in ("agent_completed", "agent_idle"):
                self.agents.pop(kind.get("agentId"), None)
            if kind_type == "shell_completed":
                self.finish(kind.get("shellId"), kind.get("exitCode"), event)
        elif event_type == "tool.execution_start" and not d.get("mcpServerName") and _SHELL_TOOL.match(d.get("toolName") or ""):
            arguments = d.get("arguments") or {}
            self.calls[d["toolCallId"]] = {
                "name": re.sub(r"^functions\.", "", d["toolName"]),
                "id": arguments.get("shellId"),
                "detached": arguments.get("detach") is True,
                "at": event["timestamp"],
            }
        elif event_type == "tool.execution_complete":
            call = self.calls.pop(d.get("toolCallId"), None)
            if not call:
                return
            result = _shell_result(d)
            call_id = (result or {}).get("id") or call.get("id") or f"call:{d.get('toolCallId')}"
            previous = self.shells.get(call_id)
            detached = (result or {}).get("detached", previous.detached if previous else call["detached"])
            if result and isinstance(result.get("exitCode"), int):
                self.finish(call_id, result["exitCode"], event)
            elif result and result.get("stopped") and call["name"] == "stop_powershell":
                self.finish(call_id, -1, event)
            elif call["name"] == "stop_powershell":
                if previous and not detached:
                    self.shells[call_id] = _Shell(previous.detached, previous.at, "unknown")
            elif result and result.get("running"):
                state = "running" if call["name"] == "powershell" or previous else "unknown"
                self.shells[call_id] = _Shell(detached, event["timestamp"], state)
            elif call["name"] == "powershell" and d.get("success") is not False:
                self.shells[call_id] = _Shell(detached, event["timestamp"], "unknown")
            elif previous and previous.state == "running":
                self.shells[call_id] = _Shell(previous.detached, previous.at, "unknown")

    def finish(self, shell_id: str | None, exit_code: Any, event: dict[str, Any]) -> None:
        previous = self.shells.get(shell_id) if shell_id else None
        if not previous:
            return
        self.shells[shell_id] = _Shell(previous.detached, previous.at, "done" if isinstance(exit_code, int) else "unknown")
        if not previous.detached and isinstance(exit_code, int) and exit_code != 0:
            self.failure = {"id": event["id"], "kind": "Background command exited unsuccessfully"}

    def snapshot(self) -> dict[str, Any]:
        shells = [item for item in self.shells.values() if not item.detached and item.state != "done"]
        running = [item for item in shells if item.state == "running"]
        times = [item.at for item in running] + list(self.agents.values())
        return {
            "backgroundCount": len(running) + len(self.agents),
            "backgroundAt": max(times) if times else None,
            "backgroundUnconfirmed": any(item.state == "unknown" for item in shells),
            "backgroundFailure": self.failure,
        }
