"""Port of src/server.mjs -- the collector-mode HTTPS/HTTP host.

This module ports the data-plane and protocol logic of server.mjs: the
authenticated HTTPS ingestion endpoints (/v1/connect, /v1/report,
/v1/disconnect), the loopback HTTP dashboard/control API (/api/status,
/api/control, /api/test, /api/stop, /api/dismiss, static assets), and the
in-process self-observation local-poll loop (mirroring watcher.py's own
poll loop, but driving `collector.connect`/`collector.accept` directly
rather than going over the network, exactly as the original does).

Deliberately deferred to Phase 3 (see docs/porting-notes.md), via the same
`TrayBridge`-style injectable seam used in watcher.py: spawning
windows/tray.ps1, delivering native toast notifications, reading the
Windows light/dark theme preference, and process-evidence collection for
self-observation. `bridge=None` is a fully valid, headless-testable mode:
notifications fall back to the JS `!bridgeReady` degraded path (queued but
marked "failed: Native notification helper unavailable"), `bridge_ready`
stays False (so `/api/status` reports every row as "unconfirmed", matching
the original's behavior whenever its tray helper hasn't attached yet), and
process evidence is simply absent (LocalSource/FamilyMonitor already
tolerate this gracefully, per Phase 1).
"""
from __future__ import annotations

import os
import socket
import ssl
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from aiohttp import web

from .actions import DismissError, MonitorActions, read_dismiss_entries
from .collector import Collector
from .configuration import (
    data_dir,
    ensure_local_reporter,
    initialize,
    load_collector,
    migrate_legacy,
    pair_connection_string,
    root,
    save_config,
)
from .engine import Ledger, SessionStore, digest
from .lifecycle import acquire_role
from .local_report import create_local_observer, local_report_payload, poll_local
from .protocol import HASH_RE, ProtocolError, authorized, fail, read_json

_PORT = int(os.environ.get("MONITOR_PORT", "43187"))
_SELF_OBSERVE = os.environ.get("MONITOR_SELF_OBSERVE", "1") != "0"
_ALERT_PREFIX = {
    "finished": "Family finished", "waiting": "Family needs attention",
    "error": "Family error", "warning": "Family status unavailable", "test": "TEST notification",
}
_STATIC_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
        "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
}


def _shorten(value: Any, length: int) -> str:
    import re

    return re.sub(r"[\x00-\x1f]", " ", str(value))[:length]


def _iso(now_ms: float | None = None) -> str:
    from datetime import datetime, timezone

    moment = datetime.fromtimestamp((now_ms if now_ms is not None else time.time() * 1000) / 1000, tz=timezone.utc)
    return moment.isoformat().replace("+00:00", "Z")


class TrayBridge(Protocol):
    """Injectable seam for the Windows tray helper (see module docstring)."""

    def process_snapshot(self) -> dict[str, Any] | None: ...

    async def notify(self, notification: dict[str, Any]) -> bool:
        """Deliver (or queue) a notification; return True if accepted for delivery."""
        ...

    async def generate_connection(self, label: str | None) -> None: ...

    async def on_stop(self) -> None:
        """Tell the bridge the collector is shutting down (mirrors watcher.py's hook)."""
        ...


class CollectorServer:
    """Port of server.mjs's top-level collector host."""

    def __init__(self, bridge: TrayBridge | None = None) -> None:
        self.bridge = bridge
        self.instance_id = str(uuid.uuid4())
        self.token = os.urandom(32).hex()
        self.port = _PORT
        self.url = f"http://127.0.0.1:{self.port}"
        self.bridge_ready = False
        self.stopping = False
        self.fault: str | None = None
        self.last_test = 0.0
        self.theme_checked_at = 0.0
        self.theme: dict[str, Any] = {
            "mode": None, "source": "browser-fallback", "reason": "Waiting for Windows app preference",
        }
        self.notification: dict[str, Any] = {"state": "starting", "message": "Starting Windows tray helper"}
        self._release: Callable[[], Awaitable[None]] | None = None
        self._dashboard_runner: web.AppRunner | None = None
        self._ingest_runners: list[web.AppRunner] = []
        self._timer_task: Any = None
        self._sse_clients: set[web.StreamResponse] = set()
        self.local_reporter_id: str | None = None
        self.processes: dict[str, Any] | None = None
        self.local_reset: str | None = None
        self.local_lease: str | None = None
        self.local_seq = 0
        self.local_polling = False
        self.local_notices: list[dict[str, Any]] = []
        self.local_observer: dict[str, Any] | None = None
        self.local_store: SessionStore | None = None

    # -- notifications -----------------------------------------------------

    async def notify(self, key: str, alert: dict[str, Any]) -> None:
        if not await self.ledger.claim_digest(key):
            return
        prefix = _ALERT_PREFIX[alert["kind"]]
        self.notification = {
            "id": str(uuid.uuid4()), "state": "queued", "kind": alert["kind"], "at": _iso(),
            "title": f"{prefix}: {_shorten(alert['title'], 140)}", "message": _shorten(alert["message"], 220),
        }
        accepted = bool(self.bridge_ready and self.bridge) and await self.bridge.notify(self.notification)
        if not accepted:
            self.notification["state"] = "failed"
            self.notification["message"] = "Native notification helper unavailable. Alert was not delivered."

    async def test_notification(self) -> None:
        now = time.time() * 1000
        if now - self.last_test < 5000:
            return
        self.last_test = now
        await self.notify(
            digest(f"test:{uuid.uuid4()}"),
            {
                "kind": "test", "title": "Copilot session monitor",
                "message": "TEST ONLY - notifications are delivered on the collector PC, "
                "independently of browsers and watchers.",
            },
        )

    # -- configuration refresh ----------------------------------------------

    async def _refresh_configuration(self) -> None:
        next_config = await load_collector()
        if (
            next_config["id"] != self.config["id"]
            or next_config["bindAddress"] != self.config["bindAddress"]
            or next_config["port"] != self.config["port"]
        ):
            raise RuntimeError("Collector listener configuration changed; restart required")
        self.config = next_config
        self.collector.configure(next_config)

    # -- self-observation local poll loop -----------------------------------

    def _process_snapshot(self) -> dict[str, Any] | None:
        return self.processes

    async def _local_poll(self) -> None:
        if not _SELF_OBSERVE or self.local_polling or self.stopping:
            return
        self.local_polling = True
        try:
            await self.actions.run(self._local_poll_body)
        except Exception as error:  # noqa: BLE001 -- mirrors JS catch-all; drop the lease and keep polling later.
            self.local_lease = None
            print(f"Local self-observation unavailable ({error})")
        finally:
            self.local_polling = False

    async def _local_poll_body(self) -> None:
        if not self.local_lease:
            # See the matching comment in watcher.py: a dropped internal
            # lease does not mean self-observation itself was interrupted,
            # so this must not force-invalidate currently-tracked sessions.
            # The collector already treats a reconnect as its own baseline.
            connected = await self.collector.connect({
                "version": 1, "reporterId": self.local_reporter_id, **self.local_identity,
            })
            self.local_lease = connected["lease"]
            self.local_seq = 0
        self.local_notices = []
        forced_gap_reason = None
        if self.local_reset:
            forced_gap_reason = self.local_reset
            self.local_reset = None
        outcome = await poll_local(self.local_observer, forced_gap_reason)
        await self.local_store.save(outcome["result"]["members"])
        payload = local_report_payload(self.local_observer["monitor"], outcome, self.local_notices)
        self.local_seq += 1
        await self.collector.accept({
            "version": 1, "reporterId": self.local_reporter_id, "lease": self.local_lease,
            "seq": self.local_seq, "sentAt": _iso(), **payload,
        })

    # -- dashboard status ----------------------------------------------------

    def status(self) -> dict[str, Any]:
        result = self.collector.snapshot()
        if not self.bridge_ready or self.fault:
            def mark(row: dict[str, Any]) -> dict[str, Any]:
                out = {
                    **row, "state": "unknown", "finishedAt": None, "dismissKey": None, "runningCount": 0,
                    "detail": "Collector unavailable; current status unconfirmed",
                }
                if row.get("members"):
                    out["members"] = [mark(member) for member in row["members"]]
                    out["relatives"] = [mark(rel) for rel in row.get("relatives", [])]
                return out

            result["sessions"] = [mark(row) for row in result["sessions"]]
            result["members"] = [mark(row) for row in result["members"]]
            result["active"] = []
            result["attention"] = result["sessions"]
            result["issues"].append(self.fault or "Native helper unavailable")
        theme = (
            self.theme
            if self.bridge_ready and time.time() * 1000 - self.theme_checked_at < 8000
            else {"mode": None, "source": "browser-fallback", "reason": "Windows theme reader unavailable"}
        )
        return {
            **result, "instanceId": self.instance_id, "machine": socket.gethostname(),
            "healthy": self.bridge_ready and not self.fault,
            "source": "Authenticated metadata reports from paired Windows watchers",
            "updatedAt": _iso(), "notification": self.notification, "theme": theme,
        }

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        data_dir.mkdir(parents=True, exist_ok=True)
        self._release = await acquire_role(data_dir, "collector")
        # Bootstraps a fresh install (first-run config + certs) and repairs a
        # missing collector-key.pem on an already-configured install (e.g. a
        # legacy Node/PFX migration), rather than requiring a separate
        # `init-host.py` run first. init-host.py remains the path for
        # choosing a non-default bind address or forcing --reconfigure.
        self.config = await initialize()
        self.local_reporter_id = await ensure_local_reporter(self.config) if _SELF_OBSERVE else None
        collector_file = str(data_dir / "collector-state.json")
        await migrate_legacy(self.config, collector_file)
        legacy = SessionStore(str(data_dir / "sessions.json"))
        retained = await legacy.load()
        self.ledger = Ledger(str(data_dir / "notifications.json"))
        await self.ledger.load()
        self.collector = Collector(collector_file, self.notify, process_started_at_ms=time.time() * 1000)
        await self.collector.load(self.config, retained, legacy.dismissed)
        self.actions = MonitorActions(
            self.collector, self._refresh_configuration, self.collector.save,
            lambda: not self.stopping and not self.fault and self.bridge_ready,
        )

        if _SELF_OBSERVE:
            self.local_store = SessionStore(str(data_dir / "collector-local-sessions.json"))
            from .configuration import optional_config

            saved_local_identity = await optional_config("collector-local-identity.json")
            legacy_watcher_identity = None if saved_local_identity else await optional_config("watcher-identity.json")
            seed = saved_local_identity or legacy_watcher_identity or {}
            self.local_identity = {
                "installationId": seed.get("installationId") or str(uuid.uuid4()),
                "generation": (seed.get("generation") or 0) + 1,
                "bootId": str(uuid.uuid4()),
            }
            await save_config("collector-local-identity.json", self.local_identity)
            self.local_observer = create_local_observer(
                socket.gethostname(), await self.local_store.load(),
                self._process_snapshot, self.local_notices.append,
            )

        await self._start_dashboard()
        await self._start_ingest_listeners()
        await save_config("runtime.json", {"pid": os.getpid(), "instanceId": self.instance_id, "url": self.url, "token": self.token})

        import asyncio

        async def _loop() -> None:
            while True:
                await asyncio.sleep(1.5)
                if self.stopping:
                    return
                await self.actions.run(self._refresh_tick)
                await self._local_poll()
                await self._broadcast_status()

        self._timer_task = asyncio.ensure_future(_loop())
        await self._local_poll()
        await self._broadcast_status()

    async def _refresh_tick(self) -> None:
        try:
            await self._refresh_configuration()
            self.collector.snapshot()
            self.fault = None
        except Exception as error:  # noqa: BLE001 -- mirrors JS catch-all configuration-refresh failure path.
            self.fault = "Collector configuration unavailable"
            print(f"{self.fault} ({error})")

    async def stop(self) -> None:
        if self.stopping:
            return
        self.stopping = True
        if self._timer_task:
            self._timer_task.cancel()
        for runner in self._ingest_runners:
            await runner.cleanup()
        if self._dashboard_runner:
            await self._dashboard_runner.cleanup()
        if self.bridge:
            await self.bridge.on_stop()
        import asyncio

        while self.local_polling:
            await asyncio.sleep(0.05)
        await self.actions.run(self._finish_stop)

    async def _finish_stop(self) -> None:
        if self.local_lease:
            try:
                self.collector.disconnect(self.local_reporter_id, self.local_lease)
            except ProtocolError:
                pass  # lease already invalid
        await self.collector.save()
        try:
            (data_dir / "runtime.json").unlink()
        except FileNotFoundError:
            pass
        if self._release:
            await self._release()

    # -- dashboard app (loopback HTTP) ---------------------------------------

    async def _start_dashboard(self) -> None:
        app = web.Application(middlewares=[self._same_origin_middleware])
        app.router.add_get("/api/status", self._handle_status)
        app.router.add_get("/api/stream", self._handle_stream)
        app.router.add_get("/api/control", self._handle_control_token)
        app.router.add_post("/api/test", self._handle_test)
        app.router.add_post("/api/stop", self._handle_stop)
        app.router.add_post("/api/dismiss", self._handle_dismiss)
        for route, (_, _) in _STATIC_ASSETS.items():
            app.router.add_get(route, self._handle_static)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", self.port)
        await site.start()
        self._dashboard_runner = runner

    @web.middleware
    async def _same_origin_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        response_headers = dict(_SECURITY_HEADERS)
        host = request.headers.get("Host")
        origin = request.headers.get("Origin")
        if (
            host not in (f"127.0.0.1:{self.port}", f"localhost:{self.port}")
            or (origin and origin not in (self.url, f"http://localhost:{self.port}"))
            or request.headers.get("Sec-Fetch-Site") == "cross-site"
        ):
            return web.json_response({"error": "Local same-origin requests only"}, status=403, headers=response_headers)
        try:
            response = await handler(request)
        except ProtocolError as error:
            response = web.json_response({"error": str(error)}, status=error.status)
        except DismissError as error:
            response = web.json_response({"error": str(error)}, status=error.status)
        except web.HTTPException:
            raise
        except Exception as error:  # noqa: BLE001 -- mirrors JS catch-all request-failure response.
            print(f"Local request failed ({error})")
            response = web.json_response({"error": "Collector request failed"}, status=500)
        for key, value in response_headers.items():
            response.headers.setdefault(key, value)
        return response

    async def _handle_status(self, request: web.Request) -> web.Response:
        return web.json_response(self.status())

    async def _handle_stream(self, request: web.Request) -> web.StreamResponse:
        import asyncio

        response = web.StreamResponse(
            headers={
                **_SECURITY_HEADERS,
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            }
        )
        await response.prepare(request)
        self._sse_clients.add(response)
        try:
            await self._sse_write(response, self.status())
            await asyncio.Event().wait()
        except ConnectionResetError:
            pass  # client dropped mid-write; disconnect is handled like any other below.
        finally:
            self._sse_clients.discard(response)
        return response

    @staticmethod
    async def _sse_write(response: web.StreamResponse, payload: dict[str, Any]) -> None:
        import json

        # `retry:` tells the browser's native EventSource reconnect backoff how
        # long to wait, keeping it close to the previous ~1.5s poll cadence.
        await response.write(f"retry: 1000\ndata: {json.dumps(payload)}\n\n".encode("utf-8"))

    async def _broadcast_status(self) -> None:
        if not self._sse_clients:
            return
        payload = self.status()
        dead: list[web.StreamResponse] = []
        for client in list(self._sse_clients):
            try:
                await self._sse_write(client, payload)
            except Exception:  # noqa: BLE001 -- a dead/dropped stream client should not block the others.
                dead.append(client)
        for client in dead:
            self._sse_clients.discard(client)

    async def _handle_control_token(self, request: web.Request) -> web.Response:
        return web.json_response({"token": self.token})

    def _require_control_token(self, request: web.Request) -> web.Response | None:
        if request.headers.get("Authorization") != f"Bearer {self.token}":
            return web.json_response({"error": "Monitor control token required"}, status=403)
        return None

    async def _handle_test(self, request: web.Request) -> web.Response:
        denied = self._require_control_token(request)
        if denied:
            return denied
        await self.test_notification()
        await self._broadcast_status()
        return web.json_response({"notification": self.notification})

    async def _handle_stop(self, request: web.Request) -> web.Response:
        denied = self._require_control_token(request)
        if denied:
            return denied
        response = web.json_response({"stopping": True})
        import asyncio

        asyncio.ensure_future(self.stop())
        return response

    async def _handle_dismiss(self, request: web.Request) -> web.Response:
        denied = self._require_control_token(request)
        if denied:
            return denied
        entries = await read_dismiss_entries(request.content.iter_any())
        result = await self.actions.dismiss(entries)
        await self._broadcast_status()
        return web.json_response(result)

    async def _handle_static(self, request: web.Request) -> web.Response:
        file, content_type = _STATIC_ASSETS[request.path]
        body = (root / "public" / file).read_bytes()
        return web.Response(body=body, content_type=content_type.split(";")[0], charset="utf-8", headers={"Cache-Control": "no-store"})

    # -- ingestion app (HTTPS, one listener per bind address) ---------------

    async def _start_ingest_listeners(self) -> None:
        app = web.Application()
        app.router.add_post("/v1/connect", self._handle_ingest)
        app.router.add_post("/v1/report", self._handle_ingest)
        app.router.add_post("/v1/disconnect", self._handle_ingest)
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        ssl_context.load_cert_chain(str(data_dir / "collector-cert.pem"), str(data_dir / "collector-key.pem"))
        for address in dict.fromkeys(["127.0.0.1", self.config["bindAddress"]]):
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, address, self.config["port"], ssl_context=ssl_context)
            await site.start()
            self._ingest_runners.append(runner)

    async def _handle_ingest(self, request: web.Request) -> web.Response:
        try:
            if request.headers.get("Origin"):
                raise fail("Reporter endpoint only", 403)
            await self._refresh_configuration()
            id_ = request.headers.get("X-Monitor-Reporter")
            pairing = self.collector.pairings.get(id_) if id_ else None
            if not pairing or not authorized(request.headers.get("Authorization"), pairing["tokenHash"]):
                raise fail("Reporter authentication required", 403)
            value = await read_json(request)
            result = await self.actions.run(lambda: self._ingest_body(request.path, id_, value))
            return web.json_response(result)
        except ProtocolError as error:
            print(f"Reporter request rejected ({error.status})")
            return web.json_response({"error": str(error)}, status=error.status)
        except Exception as error:  # noqa: BLE001 -- mirrors JS catch-all; any failure is a 503 persistence/config error.
            print(f"Reporter request rejected ({error})")
            return web.json_response({"error": "Collector persistence/configuration unavailable"}, status=503)

    async def _ingest_body(self, path: str, id_: str, value: dict[str, Any]) -> dict[str, Any]:
        await self._refresh_configuration()
        if id_ not in self.collector.pairings:
            raise fail("Reporter revoked", 403)
        if path == "/v1/disconnect":
            if len(value) != 1 or not HASH_RE.match(value.get("lease", "")):
                raise fail("Invalid disconnect")
            return self.collector.disconnect(id_, value["lease"])
        if value.get("reporterId") != id_:
            raise fail("Reporter identity mismatch", 403)
        if path == "/v1/connect":
            return await self.collector.connect(value)
        return await self.collector.accept(value)

    # -- tray-bridge event hooks (Phase 3 seam) ------------------------------

    def on_bridge_ready(self) -> None:
        self.bridge_ready = True
        self.notification = {
            "state": "ready",
            "message": "Collector tray ready; use Test notification to check Windows delivery.",
        }

    def on_theme(self, mode: str | None) -> None:
        self.theme_checked_at = time.time() * 1000
        self.theme = (
            {"mode": mode, "source": "windows-apps"} if mode in ("light", "dark")
            else {"mode": None, "source": "browser-fallback", "reason": "Windows preference unavailable"}
        )

    async def on_power_event(self) -> None:
        self.processes = None
        self.local_reset = "Windows process observation interrupted; rebaselining"

        async def _disconnect_leased_sources() -> None:
            for source in list(self.collector.sources.values()):
                if source.get("lease"):
                    self.collector.disconnect(source["id"], source["lease"])

        await self.actions.run(_disconnect_leased_sources)

    def on_bridge_fault(self, message: str = "Native helper unavailable") -> None:
        self.fault = message

    def on_bridge_lost(self) -> None:
        self.bridge_ready = False
        self.processes = None


async def pair_connection(label: str | None) -> str:
    """Thin wrapper matching server.mjs's "generate-connection" tray-event handler body."""
    import datetime

    text = (label or "").strip() or f"Sub machine {datetime.datetime.now(datetime.timezone.utc).isoformat()}"
    return await pair_connection_string(text)
