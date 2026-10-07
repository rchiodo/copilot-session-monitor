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
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

import pymonitor.server as server_module
from pymonitor.actions import MonitorActions
from pymonitor.collector import Collector
from pymonitor.engine import Ledger
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
