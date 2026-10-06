"""Port of src/actions.mjs -- serialized dismiss-request handling that
guards against racing refreshes and partial persistence failures.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable


class DismissError(Exception):
    """Mirrors the JS pattern of Object.assign(new Error(...), { status })."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class MonitorActions:
    def __init__(
        self,
        engine: Any,
        refresh: Callable[[], Awaitable[None]],
        persist: Callable[[], Awaitable[None]],
        healthy: Callable[[], bool],
    ) -> None:
        self.engine = engine
        self.refresh = refresh
        self.persist = persist
        self.healthy = healthy
        # Equivalent to the JS `this.queue = Promise.resolve()` chain: a lock
        # serializes actions (including a concurrent refresh+dismiss race)
        # without requiring an event loop at construction time.
        self._lock = asyncio.Lock()

    def run(self, action: Callable[[], Awaitable[Any]]) -> Awaitable[Any]:
        async def _run() -> Any:
            async with self._lock:
                return await action()

        return asyncio.ensure_future(_run())

    def dismiss(self, entries: list[dict[str, Any]]) -> Awaitable[dict[str, list[Any]]]:
        async def _action() -> dict[str, list[Any]]:
            await self.refresh()
            if not self.healthy():
                raise DismissError("Observer unavailable; nothing dismissed", 503)
            before = dict(self.engine.dismissed)
            try:
                result = self.engine.dismiss(entries)
                await self.persist()
                return result
            except Exception as error:
                self.engine.dismissed = before
                if isinstance(error, TypeError):
                    raise DismissError(str(error), 400) from error
                raise

        return self.run(_action)


async def read_dismiss_entries(reader: Any) -> list[dict[str, Any]]:
    """Read a dismiss request body from an async iterable of byte chunks.

    Mirrors node's `for await (const chunk of request)` over an IncomingMessage;
    callers pass anything that yields bytes-like chunks when iterated with
    `async for`.
    """
    size = 0
    chunks: list[bytes] = []
    async for chunk in reader:
        size += len(chunk)
        if size > 1048576:
            raise DismissError("Dismiss request too large", 413)
        chunks.append(bytes(chunk))
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8"))
        return payload["entries"]
    except Exception as error:
        raise DismissError("Invalid dismiss request JSON", 400) from error
