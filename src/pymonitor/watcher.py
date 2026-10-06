"""Port of src/watcher.mjs.

`Watcher` is the standalone-watcher orchestrator: durable per-installation
identity, pairing storage, the local Copilot-session observer (from
`local_report.py`), the `Reporter` HTTPS client, the ~1.5s report cadence,
and a tiny loopback-only control HTTP server (`/status`, `/connect`,
`/stop`) that the tray helper uses to show status and apply a pasted
connection string.

Scope note: this module intentionally stops at the orchestration/protocol
boundary. The Windows tray helper (`windows/tray.ps1`) -- native
notifications, the "Connect to host..."/"Stop watcher" context menu, process
evidence (`-WatcherOnly` process/power events) -- is wired in as an
injectable `TrayBridge` seam (`bridge` constructor argument) rather than
spawned here, per the Phase 2 (protocol/network layer) / Phase 3
(tray+notifications) split agreed in the plan. A `None` bridge means no
process evidence is available (`process_snapshot()` returns `None`), which
`LocalSource`/`FamilyMonitor` already handle gracefully; Phase 3 wires a real
bridge in without changing anything here.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import urlsplit

from aiohttp import web

from .configuration import data_dir, optional_config, save_config
from .engine import SessionStore
from .lifecycle import acquire_role
from .local_report import create_local_observer, local_report_payload, poll_local
from .protocol import ProtocolError, decode_connection_string
from .reporter import Reporter

__all__ = ["Watcher", "TrayBridge"]

POLL_INTERVAL_SECONDS = 1.5


class TrayBridge(Protocol):
    """Injectable seam for the Phase 3 tray helper.

    `process_snapshot()` mirrors the JS `() => processes` closure passed into
    `createLocalObserver`; everything else is a notification hook the real
    bridge can use to react to watcher state (Phase 3 wires these up to
    `tray.ps1`'s stdin/stdout protocol).
    """

    def process_snapshot(self) -> dict[str, Any] | None: ...
    async def on_health_changed(self, health: dict[str, Any]) -> None: ...
    async def on_stop(self) -> None: ...


class Watcher:
    def __init__(self, bridge: TrayBridge | None = None) -> None:
        self.bridge = bridge
        self.store = SessionStore(str(data_dir / "watcher-sessions.json"))
        self.pairing: dict[str, Any] | None = None
        self.identity: dict[str, Any] | None = None
        self.reporter: Reporter | None = None
        self.monitor = None
        self.source = None
        self._timer_task = None
        self._notices: list[dict[str, Any]] = []
        self.health: dict[str, Any] = {"healthy": False, "issue": "Starting watcher", "lastAcknowledgedAt": None}
        self.reset: str | None = None
        self._stopping = False
        self._polling = False
        self._last_error: str | None = None
        self._release: Callable[[], Awaitable[None]] | None = None
        self.app = web.Application(middlewares=[self._same_origin])
        self.control_token = secrets.token_hex(32)
        self.app.router.add_get("/status", self._handle_status)
        self.app.router.add_post("/connect", self._handle_connect)
        self.app.router.add_post("/stop", self._handle_stop)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self.port: int | None = None

    # -- lifecycle -----------------------------------------------------

    async def start(self) -> None:
        self._release = await acquire_role(data_dir, "watcher")
        self.pairing = await optional_config("watcher.json")
        saved_identity = await optional_config("watcher-identity.json")
        self.identity = {
            "installationId": (saved_identity or {}).get("installationId") or str(uuid.uuid4()),
            "generation": ((saved_identity or {}).get("generation") or 0) + 1,
            "bootId": str(uuid.uuid4()),
        }
        await save_config("watcher-identity.json", self.identity)
        self.health = (
            {"healthy": False, "issue": "Starting watcher", "lastAcknowledgedAt": None}
            if self.pairing
            else {
                "healthy": False,
                "issue": 'Waiting to be paired with a host (use the tray "Connect to host..." menu)',
                "lastAcknowledgedAt": None,
            }
        )
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        self.port = self._site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        await save_config(
            "watcher-runtime.json",
            {"pid": 0, "instanceId": self.identity["bootId"], "url": f"http://127.0.0.1:{self.port}", "token": self.control_token},
        )
        if self.pairing:
            await self.begin_reporting(self.pairing)

    async def stop(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        if self._timer_task:
            self._timer_task.cancel()
        while self._polling:
            await asyncio.sleep(0.05)
        if self.reporter:
            try:
                await self.reporter.disconnect()
            except Exception as error:  # noqa: BLE001 - mirrors JS best-effort disconnect logging
                print(f"Collector disconnect not acknowledged ({getattr(error, 'status', None) or type(error).__name__})")
        if self.bridge:
            await self.bridge.on_stop()
        if self._runner:
            await self._runner.cleanup()
        runtime_file = data_dir / "watcher-runtime.json"
        if runtime_file.exists():
            runtime_file.unlink()
        if self._release:
            await self._release()

    # -- reporting loop --------------------------------------------------

    async def poll(self) -> None:
        if self._polling or self._stopping or not self.reporter:
            return
        self._polling = True
        try:
            # Reconnecting after a dropped reporting lease does not mean local
            # observation itself was interrupted, so this must not
            # force-invalidate currently-tracked sessions (see
            # test_reconnect_lease.py / docs/porting-notes.md). The collector
            # already treats a reconnect as its own baseline.
            if not self.reporter.lease:
                await self.reporter.connect()
            self._notices = []
            forced_gap_reason = None
            if self.reset:
                forced_gap_reason = self.reset
                self.reset = None
            outcome = await poll_local({"source": self.source, "monitor": self.monitor}, forced_gap_reason)
            await self.store.save(outcome["result"]["members"])
            response = await self.reporter.send(local_report_payload(self.monitor, outcome, self._notices))
            self.health = {
                "healthy": outcome["healthy"] and response.get("healthy") is not False,
                "issue": None if outcome["healthy"] else "; ".join(outcome["issues"]),
                "lastAcknowledgedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            }
            self._last_error = None
        except Exception as error:  # noqa: BLE001 - mirrors JS catch-all; any failure drops the lease.
            if self.reporter:
                self.reporter.lease = None
            message = error.args[0] if isinstance(error, ProtocolError) and error.args else None
            issue = message or f"Reporting unavailable ({type(error).__name__})"
            self.health = {**self.health, "healthy": False, "issue": issue}
            if self.health["issue"] != self._last_error:
                print(self.health["issue"])
            self._last_error = self.health["issue"]
        finally:
            self._polling = False
        if self.bridge:
            await self.bridge.on_health_changed(self.health)

    async def begin_reporting(self, next_pairing: dict[str, Any]) -> None:
        if self._timer_task:
            self._timer_task.cancel()
            self._timer_task = None
        if self.reporter:
            try:
                await self.reporter.disconnect()
            except Exception as error:  # noqa: BLE001 - best-effort disconnect, mirrors JS
                print(f"Collector disconnect not acknowledged ({getattr(error, 'status', None) or type(error).__name__})")
        self.pairing = next_pairing
        self.reporter = Reporter(next_pairing, self.identity)
        observer = create_local_observer(
            next_pairing["label"],
            await self.store.load(),
            (self.bridge.process_snapshot if self.bridge else (lambda: None)),
            self._notices.append,
        )
        self.monitor = observer["monitor"]
        self.source = observer["source"]
        self._notices = []
        self.health = {"healthy": False, "issue": "Starting watcher", "lastAcknowledgedAt": None}

        async def _loop() -> None:
            while True:
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                await self.poll()

        self._timer_task = asyncio.ensure_future(_loop())
        await self.poll()

    async def apply_connection_string(self, value: str) -> dict[str, Any]:
        next_pairing = decode_connection_string(value)
        if self.pairing and self.pairing["reporterId"] != next_pairing["reporterId"]:
            raise RuntimeError(
                "This watcher is already paired with a different host; use a separate installation directory"
            )
        await save_config("watcher.json", next_pairing)
        await self.begin_reporting(next_pairing)
        return {"label": next_pairing["label"], "host": urlsplit(next_pairing["collectorUrl"]).netloc}

    # -- control HTTP server ---------------------------------------------

    @web.middleware
    async def _same_origin(self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.Response]]) -> web.Response:
        if (
            request.headers.get("Host") != f"127.0.0.1:{self.port}"
            or request.headers.get("Origin")
            or request.headers.get("Sec-Fetch-Site") == "cross-site"
        ):
            return web.json_response({}, status=403)
        return await handler(request)

    async def _handle_status(self, request: web.Request) -> web.Response:
        return web.json_response({
            **self.health,
            "instanceId": self.identity["bootId"] if self.identity else None,
            "reporterId": (self.pairing or {}).get("reporterId"),
            "paired": bool(self.pairing),
        })

    async def _handle_connect(self, request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != f"Bearer {self.control_token}":
            return web.json_response({}, status=403)
        body = await request.content.read(65536 + 1)
        if len(body) > 65536:
            return web.json_response({"ok": False, "message": "Request too large"}, status=413)
        try:
            payload = json.loads(body.decode("utf-8"))
            result = await self.apply_connection_string(payload["value"])
            return web.json_response({"ok": True, **result})
        except ProtocolError as error:
            return web.json_response({"ok": False, "message": str(error)}, status=error.status)
        except Exception as error:  # noqa: BLE001 - mirrors JS catch-all response shape
            return web.json_response({"ok": False, "message": str(error)}, status=400)

    async def _handle_stop(self, request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != f"Bearer {self.control_token}":
            return web.json_response({}, status=403)
        response = web.json_response({"stopping": True})
        asyncio.ensure_future(self.stop())
        return response
