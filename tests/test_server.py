"""Tests for pymonitor.server's loopback dashboard/control API (port of
server.mjs's `/api/*` surface and same-origin guard).

Scoped deliberately like test_watcher.py: this builds a `CollectorServer`
instance wired to a real in-memory `Collector`/`Ledger`/`MonitorActions`
(same fixture pattern as test_collector.py), but WITHOUT calling
`CollectorServer.start()` -- that method binds real TLS ingest listeners
and needs on-disk certs/config, which is out of scope here (per the Phase 2
testing-strategy decision recorded in docs/porting-notes.md: in-process
aiohttp TestServer/TestClient over a full process-spawn/real-TLS
replication of the original test/controls.test.mjs). Instead, the
dashboard's `web.Application` (same-origin middleware + /api/* routes) is
built the same way `_start_dashboard()` builds it and served via aiohttp's
in-process TestServer/TestClient.

Collector-level protocol/trust-state behavior (namespacing, lease fencing,
zombie-row rescue, clock skew, etc. -- the user's named "unconfirmed
status" / "zombie row" / "family infection" regression concerns) is already
fully covered by test_collector.py; this suite only exercises server.py's
own wiring: the same-origin guard, control-token gating, dismiss/test/stop
routing, and the bridge-unavailable "unconfirmed status" degradation path
(bridge=None / bridge_ready=False), which is server.py-specific and not
covered anywhere else.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

import pymonitor.server as server_module
from pymonitor.actions import MonitorActions
from pymonitor.collector import Collector
from pymonitor.engine import Ledger
from pymonitor.protocol import HEARTBEAT_MS, digest
from pymonitor.server import CollectorServer, _STATIC_ASSETS


def _config() -> dict[str, Any]:
    return {
        "reporters": [
            {"id": str(uuid.uuid4()), "label": "test-reporter", "tokenHash": "a" * 64, "legacy": False},
        ],
    }


async def _make_server(tmp_path: Path) -> CollectorServer:
    server = CollectorServer(bridge=None)
    server.config = _config()
    server.ledger = Ledger(str(tmp_path / "notifications.json"))
    await server.ledger.load()
    server.collector = Collector(str(tmp_path / "collector-state.json"), server.notify)
    await server.collector.load(server.config, [], {})
    server.actions = MonitorActions(
        server.collector,
        AsyncMock(),
        server.collector.save,
        lambda: not server.stopping and not server.fault and server.bridge_ready,
    )
    return server


def _build_dashboard_app(server: CollectorServer) -> web.Application:
    """Mirrors CollectorServer._start_dashboard()'s route table, minus the
    real TCPSite bind (tests host it via aiohttp's TestServer instead)."""
    app = web.Application(middlewares=[server._same_origin_middleware])
    app.router.add_get("/api/status", server._handle_status)
    app.router.add_get("/api/stream", server._handle_stream)
    app.router.add_get("/api/control", server._handle_control_token)
    app.router.add_post("/api/test", server._handle_test)
    app.router.add_post("/api/stop", server._handle_stop)
    app.router.add_post("/api/dismiss", server._handle_dismiss)
    for route in _STATIC_ASSETS:
        app.router.add_get(route, server._handle_static)
    return app


class _DashboardClient:
    """Thin wrapper that always attaches a matching Host header, since the
    same-origin middleware checks it against `self.port` -- only known once
    the TestServer has actually bound its ephemeral port (see the `dashboard`
    fixture below), exactly like test_watcher.py's `_ControlClient`.
    """

    def __init__(self, server: CollectorServer, client: TestClient) -> None:
        self.server = server
        self.client = client

    async def request(self, method: str, path: str, *, host: str | None = None, **kwargs: Any):
        headers = dict(kwargs.pop("headers", None) or {})
        headers.setdefault("Host", host if host is not None else f"127.0.0.1:{self.server.port}")
        return await self.client.request(method, path, headers=headers, **kwargs)


@pytest.fixture
async def dashboard(tmp_path: Path):
    server = await _make_server(tmp_path)
    app = _build_dashboard_app(server)
    client = TestClient(TestServer(app))
    await client.start_server()
    server.port = client.server.port
    server.url = f"http://127.0.0.1:{server.port}"
    try:
        yield server, _DashboardClient(server, client)
    finally:
        await client.close()


# -- same-origin middleware ---------------------------------------------------


async def test_status_rejects_wrong_host_header(dashboard) -> None:
    _server, client = dashboard
    resp = await client.request("GET", "/api/status", host="example.com")
    assert resp.status == 403


async def test_status_rejects_cross_site_sec_fetch(dashboard) -> None:
    _server, client = dashboard
    resp = await client.request("GET", "/api/status", headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status == 403


async def test_status_rejects_mismatched_origin(dashboard) -> None:
    server, client = dashboard
    resp = await client.request("GET", "/api/status", headers={"Origin": "http://evil.example"})
    assert resp.status == 403
    assert server.url  # sanity: self.url was set by the fixture


async def test_status_allows_matching_localhost_origin(dashboard) -> None:
    server, client = dashboard
    resp = await client.request("GET", "/api/status", headers={"Origin": server.url})
    assert resp.status == 200


# -- /api/status degraded-without-bridge contract -----------------------------


async def test_status_is_unconfirmed_without_a_ready_bridge(dashboard) -> None:
    _server, client = dashboard
    resp = await client.request("GET", "/api/status")
    assert resp.status == 200
    body = await resp.json()
    assert body["healthy"] is False
    assert body["active"] == []
    assert "Native helper unavailable" in body["issues"]
    assert body["theme"]["source"] == "browser-fallback"


async def test_status_becomes_healthy_once_bridge_is_ready(dashboard) -> None:
    server, client = dashboard
    server.on_bridge_ready()
    resp = await client.request("GET", "/api/status")
    body = await resp.json()
    assert body["healthy"] is True
    assert body["notification"]["state"] == "ready"


async def test_status_exposes_bounded_privacy_safe_reporter_diagnostics(dashboard) -> None:
    server, client = dashboard
    reporter_id = server.config["reporters"][0]["id"]
    ws = object()
    secret_lease = "c" * 64
    for index in range(30):
        server._record_reporter_event(
            reporter_id,
            "ws-1",
            ws,
            route="/v1/report",
            stage="action-complete",
            outcome="rejected",
            reason="lease-mismatch",
            value={"lease": secret_lease, "generation": index, "bootId": str(uuid.uuid4())},
        )

    resp = await client.request("GET", "/api/status")
    body = await resp.json()
    events = body["diagnostics"]["recentReporterEvents"]
    assert len(events) == 24
    assert events[-1]["reason"] == "lease-mismatch"
    assert events[-1]["generation"] == 29
    assert secret_lease not in json.dumps(events)
    assert server.config["reporters"][0]["tokenHash"] not in json.dumps(events)


async def test_existing_rows_marked_unconfirmed_until_bridge_ready(dashboard) -> None:
    server, client = dashboard
    reporter_id = server.config["reporters"][0]["id"]
    # Use a fixed "now" consistently for connect/accept and the member's own
    # timestamps so the collector's +/-30s clock-skew check (the thing that
    # otherwise forces source["healthy"] = False regardless of bridge
    # readiness) doesn't fire -- matching test_collector.py's Fixture pattern.
    now = time.time() * 1000
    at = datetime.fromtimestamp(now / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    identity = {
        "version": 1, "reporterId": reporter_id, "installationId": str(uuid.uuid4()),
        "bootId": str(uuid.uuid4()), "generation": 1,
    }
    await server.collector.connect(identity, now)
    source = server.collector.sources[reporter_id]
    from pymonitor.protocol import metadata

    await server.collector.accept({
        "version": 1, "reporterId": reporter_id, "lease": source["lease"], "seq": source["seq"] + 1,
        "sentAt": at, "healthy": True, "issues": [], "notices": [],
        **metadata({"members": [{
            "id": "parent", "parentId": None, "title": "Synthetic", "machine": "SYNTHETIC",
            "source": "Copilot desktop", "state": "working", "detail": "Synthetic state",
            "activity": "Executing tools", "runId": "run-parent",
            "firstObservedAt": at, "startedAt": at,
            "lastResponseAt": at, "lastEventAt": at,
            "finishedAt": None, "hierarchyIssue": None, "contextOnly": False, "lastAlert": None,
        }]}),
    }, now)

    resp = await client.request("GET", "/api/status")
    body = await resp.json()
    assert len(body["sessions"]) == 1
    row = body["sessions"][0]
    assert row["state"] == "unknown"
    assert row["detail"] == "Collector unavailable; current status unconfirmed"
    assert body["active"] == []
    assert body["attention"] == body["sessions"]

    server.on_bridge_ready()
    resp2 = await client.request("GET", "/api/status")
    body2 = await resp2.json()
    assert body2["sessions"][0]["state"] == "working"
    assert body2["active"] != []


# -- /api/control --------------------------------------------------------------


async def test_control_token_endpoint_returns_the_dashboard_token(dashboard) -> None:
    server, client = dashboard
    resp = await client.request("GET", "/api/control")
    assert resp.status == 200
    body = await resp.json()
    assert body["token"] == server.token


# -- /api/test, /api/stop, /api/dismiss bearer-token gating -------------------


async def test_test_notification_requires_correct_control_token(dashboard) -> None:
    server, client = dashboard
    denied = await client.request("POST", "/api/test", headers={"Authorization": "Bearer wrong"})
    assert denied.status == 403

    missing = await client.request("POST", "/api/test")
    assert missing.status == 403

    accepted = await client.request("POST", "/api/test", headers={"Authorization": f"Bearer {server.token}"})
    assert accepted.status == 200
    body = await accepted.json()
    assert "notification" in body


async def test_stop_requires_control_token_and_begins_shutdown(dashboard) -> None:
    server, client = dashboard
    denied = await client.request("POST", "/api/stop", headers={"Authorization": "Bearer wrong"})
    assert denied.status == 403

    resp = await client.request("POST", "/api/stop", headers={"Authorization": f"Bearer {server.token}"})
    assert resp.status == 200
    body = await resp.json()
    assert body["stopping"] is True
    # Let the fire-and-forget `asyncio.ensure_future(self.stop())` task run to
    # completion so it doesn't linger as a dangling task past the test.
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_dismiss_requires_control_token(dashboard) -> None:
    _server, client = dashboard
    resp = await client.request(
        "POST", "/api/dismiss", headers={"Authorization": "Bearer wrong"}, json={"entries": []},
    )
    assert resp.status == 403


async def test_dismiss_rejects_when_bridge_not_ready(dashboard) -> None:
    server, client = dashboard
    resp = await client.request(
        "POST", "/api/dismiss", headers={"Authorization": f"Bearer {server.token}"}, json={"entries": []},
    )
    assert resp.status == 503
    body = await resp.json()
    assert "Observer unavailable" in body["error"]


async def test_dismiss_rejects_empty_entries_once_bridge_ready(dashboard) -> None:
    server, client = dashboard
    server.on_bridge_ready()
    resp = await client.request(
        "POST", "/api/dismiss", headers={"Authorization": f"Bearer {server.token}"}, json={"entries": []},
    )
    assert resp.status == 400


async def test_dismiss_skips_unknown_ids_once_bridge_ready(dashboard) -> None:
    server, client = dashboard
    server.on_bridge_ready()
    resp = await client.request(
        "POST", "/api/dismiss", headers={"Authorization": f"Bearer {server.token}"},
        json={"entries": [{"id": "does-not-exist", "key": "a" * 64}]},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["dismissed"] == []
    assert len(body["skipped"]) == 1
    assert body["skipped"][0]["id"] == "does-not-exist"


# -- /api/stream (SSE) ----------------------------------------------------------


async def _read_sse_frame(resp) -> dict[str, Any]:
    """Reads one `retry: ...\\ndata: {...}\\n\\n` frame off an open stream
    response and returns the decoded JSON payload."""
    retry_line = await resp.content.readline()
    assert retry_line == b"retry: 1000\n"
    data_line = await resp.content.readline()
    assert data_line.startswith(b"data: ")
    blank_line = await resp.content.readline()
    assert blank_line == b"\n"
    return json.loads(data_line[len(b"data: "):])


async def test_stream_delivers_initial_status_frame_then_closes_cleanly(dashboard) -> None:
    server, client = dashboard
    resp = await client.request("GET", "/api/stream")
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith("text/event-stream")
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["X-Accel-Buffering"] == "no"
    assert len(server._sse_clients) == 1

    payload = await _read_sse_frame(resp)
    expected = server.status()
    # `updatedAt` is independently stamped on each call; compare everything else exactly.
    payload.pop("updatedAt")
    expected.pop("updatedAt")
    assert payload == expected

    resp.close()
    await asyncio.sleep(0.1)
    assert server._sse_clients == set()


async def test_stream_broadcasts_a_fresh_frame_after_test_notification(dashboard) -> None:
    server, client = dashboard
    stream_resp = await client.request("GET", "/api/stream")
    first = await _read_sse_frame(stream_resp)

    accepted = await client.request("POST", "/api/test", headers={"Authorization": f"Bearer {server.token}"})
    assert accepted.status == 200

    second = await _read_sse_frame(stream_resp)
    assert second["updatedAt"] != first["updatedAt"]
    assert second["notification"]["message"] == (await accepted.json())["notification"]["message"]

    stream_resp.close()


async def test_stream_broadcasts_after_dismiss(dashboard) -> None:
    server, client = dashboard
    server.on_bridge_ready()
    stream_resp = await client.request("GET", "/api/stream")
    first = await _read_sse_frame(stream_resp)

    resp = await client.request(
        "POST", "/api/dismiss", headers={"Authorization": f"Bearer {server.token}"},
        json={"entries": [{"id": "does-not-exist", "key": "a" * 64}]},
    )
    assert resp.status == 200

    second = await _read_sse_frame(stream_resp)
    assert second["updatedAt"] != first["updatedAt"]

    stream_resp.close()


# -- /v1/* ingestion ---------------------------------------------------------


def _build_ingest_app(server: CollectorServer) -> web.Application:
    app = web.Application()
    app.router.add_get("/v1/socket", server._handle_socket)
    return app


@pytest.fixture
async def ingest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    server = await _make_server(tmp_path)
    server.config["id"] = "test-instance"
    server.config["bindAddress"] = "127.0.0.1"
    server.config["port"] = 0
    monkeypatch.setattr(server_module, "load_collector", AsyncMock(return_value=server.config))
    app = _build_ingest_app(server)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield server, client
    finally:
        await client.close()


async def _send(ws, route: str, body: dict[str, Any]) -> dict[str, Any]:
    """Send one {route, body} request frame over an already-connected
    reporter socket and return a response dict with a `.status`-like key so
    assertions below read the same way they did against the old per-call
    POST responses."""
    await ws.send_json({"route": route, "body": body})
    message = await ws.receive_json()
    status = 200 if message.get("ok") else message.get("status", 400)
    return {"status": status, **message}


def _report(reporter_id: str, lease: str, seq: int) -> dict[str, Any]:
    sent_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return {
        "version": 1, "reporterId": reporter_id, "lease": lease, "seq": seq,
        "sentAt": sent_at, "healthy": True, "issues": [],
        "members": [], "relatives": [], "notices": [],
    }


def _authorize_reporter(server: CollectorServer) -> tuple[str, dict[str, str]]:
    reporter_id = server.config["reporters"][0]["id"]
    token = "shared-secret"
    server.config["reporters"][0]["tokenHash"] = digest(token)
    server.collector.pairings[reporter_id]["tokenHash"] = digest(token)
    return reporter_id, {"X-Monitor-Reporter": reporter_id, "Authorization": f"Bearer {token}"}


async def test_successful_report_binds_existing_lease_to_new_socket(ingest) -> None:
    server, client = ingest
    reporter_id, headers = _authorize_reporter(server)
    connected = await server.collector.connect({
        "version": 1, "reporterId": reporter_id, "installationId": str(uuid.uuid4()),
        "bootId": str(uuid.uuid4()), "generation": 1,
    })

    async with client.ws_connect("/v1/socket", headers=headers) as ws:
        baseline = await _send(ws, "/v1/report", _report(reporter_id, connected["lease"], 1))
        assert baseline["status"] == 200

        server.collector.sources[reporter_id]["lastSeen"] = time.time() * 1000 - HEARTBEAT_MS - 1
        late = await _send(ws, "/v1/report", _report(reporter_id, connected["lease"], 2))
        assert late["status"] == 200


async def test_new_socket_after_transport_loss_rejects_stale_persisted_lease(ingest) -> None:
    server, client = ingest
    reporter_id, headers = _authorize_reporter(server)
    connect_payload = {
        "version": 1, "reporterId": reporter_id, "installationId": str(uuid.uuid4()),
        "bootId": str(uuid.uuid4()), "generation": 1,
    }

    async with client.ws_connect("/v1/socket", headers=headers) as old_ws:
        connected = await _send(old_ws, "/v1/connect", connect_payload)
        assert connected["status"] == 200
        lease = connected["body"]["lease"]
        baseline = await _send(old_ws, "/v1/report", _report(reporter_id, lease, 1))
        assert baseline["status"] == 200

    server.collector.sources[reporter_id]["lastSeen"] = time.time() * 1000 - HEARTBEAT_MS - 1

    async with client.ws_connect("/v1/socket", headers=headers) as new_ws:
        rejected = await _send(new_ws, "/v1/report", _report(reporter_id, lease, 2))
        assert rejected["status"] == 409
        assert rejected["error"] == "Lease expired; reconnect and baseline"


async def test_socket_diagnostics_capture_acceptance_and_typed_409_ordering(ingest) -> None:
    server, client = ingest
    reporter_id, headers = _authorize_reporter(server)
    connect_payload = {
        "version": 1, "reporterId": reporter_id, "installationId": str(uuid.uuid4()),
        "bootId": str(uuid.uuid4()), "generation": 7,
    }

    async with client.ws_connect("/v1/socket", headers=headers) as ws:
        connected = await _send(ws, "/v1/connect", connect_payload)
        lease = connected["body"]["lease"]
        accepted = await _send(ws, "/v1/report", _report(reporter_id, lease, 1))
        assert accepted["status"] == 200
        rejected = await _send(ws, "/v1/report", _report(reporter_id, "f" * 64, 2))
        assert rejected["status"] == 409

    events = server.status()["diagnostics"]["recentReporterEvents"]
    assert any(
        event["route"] == "/v1/connect"
        and event["stage"] == "action-complete"
        and event["outcome"] == "accepted"
        for event in events
    )
    assert any(
        event["route"] == "/v1/report"
        and event["stage"] == "action-complete"
        and event["outcome"] == "accepted"
        for event in events
    )
    rejection = next(
        event for event in events
        if event["route"] == "/v1/report"
        and event["stage"] == "action-complete"
        and event["outcome"] == "rejected"
    )
    assert rejection["reason"] == "lease-mismatch"
    assert rejection["generation"] == 7
    assert rejection["bootIdPrefix"] == connect_payload["bootId"][:8]
    assert any(event["route"] == "socket" and event["stage"] == "close" for event in events)
    assert lease not in json.dumps(events)


async def test_successful_connect_binds_new_lease_to_socket(ingest) -> None:
    server, client = ingest
    reporter_id, headers = _authorize_reporter(server)
    connect_payload = {
        "version": 1, "reporterId": reporter_id, "installationId": str(uuid.uuid4()),
        "bootId": str(uuid.uuid4()), "generation": 1,
    }

    async with client.ws_connect("/v1/socket", headers=headers) as ws:
        connected = await _send(ws, "/v1/connect", connect_payload)
        assert connected["status"] == 200
        lease = connected["body"]["lease"]

        server.collector.sources[reporter_id]["lastSeen"] = time.time() * 1000 - HEARTBEAT_MS - 1
        accepted = await _send(ws, "/v1/report", _report(reporter_id, lease, 1))
        assert accepted["status"] == 200


async def test_self_healing_lease_expiration_stays_silent_across_reconnect(ingest, capsys) -> None:
    server, client = ingest
    reporter_id = server.config["reporters"][0]["id"]
    token = "shared-secret"
    server.config["reporters"][0]["tokenHash"] = digest(token)
    server.collector.pairings[reporter_id]["tokenHash"] = digest(token)
    headers = {"X-Monitor-Reporter": reporter_id, "Authorization": f"Bearer {token}"}

    # A lease that can never match the collector's freshly-issued one, so
    # every /v1/report below fails with the same "Lease expired" message --
    # simulating the real-world race (e.g. two watcher instances) that keeps
    # reports failing even immediately after a successful reconnect.
    bad_report = {
        "version": 1, "reporterId": reporter_id, "lease": "f" * 64, "seq": 1,
        "sentAt": "2026-01-01T12:00:00.000Z", "healthy": True, "issues": [],
        "members": [], "relatives": [], "notices": [],
    }

    async with client.ws_connect("/v1/socket", headers=headers) as ws:
        capsys.readouterr()
        first = await _send(ws, "/v1/report", bad_report)
        assert first["status"] == 409
        assert capsys.readouterr().out == ""

        # Repeating this expected self-healing failure must also stay silent.
        again = await _send(ws, "/v1/report", bad_report)
        assert again["status"] == 409
        assert capsys.readouterr().out == ""

        connect_payload = {
            "version": 1, "reporterId": reporter_id, "installationId": str(uuid.uuid4()),
            "bootId": str(uuid.uuid4()), "generation": 1,
        }
        connected = await _send(ws, "/v1/connect", connect_payload)
        assert connected["status"] == 200
        # Reconnecting is part of Reporter.send's automatic recovery.
        assert capsys.readouterr().out == ""

        # The retried report still uses the stale lease in this synthetic case,
        # and the exact self-healing rejection remains silent.
        retried = await _send(ws, "/v1/report", bad_report)
        assert retried["status"] == 409
        assert capsys.readouterr().out == ""

        rejected = await _send(ws, "/v1/report", {**bad_report, "reporterId": str(uuid.uuid4())})
        assert rejected["status"] == 403
        assert "Reporter identity mismatch" in capsys.readouterr().out


async def test_reconnect_closes_previous_socket_only_after_new_one_proves_itself(ingest) -> None:
    """A reporter that redials (e.g. after a transport failure) opens a new
    socket while its old one may still be registered. The old socket must
    stay alive merely from the new one connecting, and only get closed once
    the new socket has successfully handled one message -- never tearing
    down a healthy connection just because a second one showed up but never
    spoke."""
    server, client = ingest
    reporter_id = server.config["reporters"][0]["id"]
    token = "shared-secret"
    server.config["reporters"][0]["tokenHash"] = digest(token)
    server.collector.pairings[reporter_id]["tokenHash"] = digest(token)
    auth_value = "Bearer " + token
    headers = {"X-Monitor-Reporter": reporter_id, "Authorization": auth_value}
    connect_payload = {
        "version": 1, "reporterId": reporter_id, "installationId": str(uuid.uuid4()),
        "bootId": str(uuid.uuid4()), "generation": 1,
    }

    old_ws = await client.ws_connect("/v1/socket", headers=headers)
    new_ws = None
    try:
        first = await _send(old_ws, "/v1/connect", connect_payload)
        assert first["status"] == 200
        assert old_ws.closed is False

        new_ws = await client.ws_connect("/v1/socket", headers=headers)
        # Opening the new socket alone must not evict the still-healthy old one.
        assert old_ws.closed is False

        second = await _send(new_ws, "/v1/connect", connect_payload)
        assert second["status"] == 200

        # Only now, after the new socket proved itself, is the old one closed.
        close_message = await old_ws.receive(timeout=1)
        assert close_message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED)
        assert old_ws.closed is True
    finally:
        if new_ws is not None:
            await new_ws.close()
        if not old_ws.closed:
            await old_ws.close()


async def test_socket_send_failure_on_disconnect_does_not_crash_handler(ingest, capsys, monkeypatch) -> None:
    """If the reporter's transport drops out from under us while we're
    replying (process exit, network blip -- the real-world "Cannot write
    to closing transport" case), the handler must not crash with an
    unhandled second-send exception, and must not log a misleading
    "rejected" message: nothing was actually rejected, the socket is just
    gone."""
    server, client = ingest
    reporter_id = server.config["reporters"][0]["id"]
    token = "shared-secret"
    server.config["reporters"][0]["tokenHash"] = digest(token)
    server.collector.pairings[reporter_id]["tokenHash"] = digest(token)
    auth_value = "Bearer " + token
    headers = {"X-Monitor-Reporter": reporter_id, "Authorization": auth_value}
    connect_payload = {
        "version": 1, "reporterId": reporter_id, "installationId": str(uuid.uuid4()),
        "bootId": str(uuid.uuid4()), "generation": 1,
    }

    async def _failing_send_json(self, *args, **kwargs):
        raise ConnectionResetError("Cannot write to closing transport")

    monkeypatch.setattr(web.WebSocketResponse, "send_json", _failing_send_json)

    capsys.readouterr()
    async with client.ws_connect("/v1/socket", headers=headers) as ws:
        await ws.send_json({"route": "/v1/connect", "body": connect_payload})
        # The server can never reply (send_json is patched to always fail),
        # so the only thing the test client can observe is the server
        # giving up on the socket cleanly -- not an unhandled exception
        # that aiohttp would otherwise log with a traceback.
        message = await ws.receive(timeout=1)
        assert message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR)

    assert "Reporter request rejected" not in capsys.readouterr().out


async def test_socket_receive_reset_does_not_escape_but_application_errors_do(ingest, monkeypatch) -> None:
    server, _client = ingest
    reporter_id = server.config["reporters"][0]["id"]
    token = "shared-secret"
    server.config["reporters"][0]["tokenHash"] = digest(token)
    server.collector.pairings[reporter_id]["tokenHash"] = digest(token)
    request = SimpleNamespace(
        headers={"X-Monitor-Reporter": reporter_id, "Authorization": "Bearer " + token},
    )

    class _FailingWebSocketResponse:
        error: Exception = ConnectionResetError("Cannot write to closing transport")

        def __init__(self, **_kwargs):
            pass

        async def prepare(self, _request):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise self.error

    monkeypatch.setattr(web, "WebSocketResponse", _FailingWebSocketResponse)

    response = await server._handle_socket(request)
    assert isinstance(response, _FailingWebSocketResponse)

    _FailingWebSocketResponse.error = RuntimeError("application failure")
    with pytest.raises(RuntimeError, match="application failure"):
        await server._handle_socket(request)



# -- static assets --------------------------------------------------------------


async def test_dashboard_serves_index_html(dashboard) -> None:
    _server, client = dashboard
    resp = await client.request("GET", "/")
    assert resp.status == 200
    assert resp.headers["Cache-Control"] == "no-store"
    text = await resp.text()
    assert "<html" in text.lower()


# -- start() configuration bootstrap ---------------------------------------
#
# Scoped per this file's docstring: start() itself (real TLS listeners,
# on-disk certs/config, the role lock, the local-poll loop) is out of scope
# for an end-to-end test here. This narrowly proves the one call-site swap:
# start() must bootstrap its config via `configuration.initialize()` (which
# creates a fresh install or repairs a missing collector-key.pem from a
# legacy PFX) rather than `load_collector()` (read+validate only, which
# would raise/crash on a missing config or missing certs). Everything past
# that point is short-circuited via a sentinel exception so the test never
# touches the real role lock, TLS, or local-poll machinery.


class _StoppedAfterInitialize(Exception):
    """Sentinel raised immediately after start() calls ensure_local_reporter,
    i.e. right after the config-bootstrap line under test, so the rest of
    start()'s real role-lock/TLS/local-poll machinery is never reached."""


async def test_start_bootstraps_config_via_initialize_not_load_collector(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = CollectorServer(bridge=None)
    monkeypatch.setattr(server_module, "data_dir", tmp_path)
    monkeypatch.setattr(server_module, "acquire_role", AsyncMock(return_value=AsyncMock()))

    expected_config = _config()
    initialize_mock = AsyncMock(return_value=expected_config)
    monkeypatch.setattr(server_module, "initialize", initialize_mock)
    monkeypatch.setattr(
        server_module,
        "load_collector",
        AsyncMock(side_effect=AssertionError("start() must bootstrap via initialize(), not load_collector()")),
    )
    monkeypatch.setattr(server_module, "ensure_local_reporter", AsyncMock(side_effect=_StoppedAfterInitialize()))

    with pytest.raises(_StoppedAfterInitialize):
        await server.start()

    initialize_mock.assert_awaited_once_with()
    assert server.config == expected_config
