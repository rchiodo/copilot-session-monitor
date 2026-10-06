"""Tests for pymonitor.watcher (port of src/watcher.mjs).

No dedicated watcher.test.mjs existed in the Node app (watcher.mjs was only
exercised indirectly via test/controls.test.mjs's spawned-process fixtures),
so this suite is newly authored. It covers watcher.py's own
responsibilities directly:

  * durable per-installation identity (installationId persists across
    restarts, generation increments, bootId is fresh each boot)
  * the "waiting to be paired" vs "starting" initial health states
  * apply_connection_string's reject-on-different-reporter guard
  * the loopback control HTTP server's same-origin and bearer-token gating
    for /status, /connect, /stop

`create_local_observer`/`poll_local`/`Reporter` are stubbed out everywhere
here so these tests exercise watcher.py's own orchestration logic without
needing a real Copilot desktop data.db, SDK client, or TLS collector --
LocalSource/FamilyMonitor are already covered by test_source.py/
test_families.py/test_reconnect_lease.py, and Reporter by test_reporter.py.
"""
from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from pymonitor import configuration as cfg
from pymonitor import watcher as watcher_mod
from pymonitor.protocol import VERSION, encode_connection_string
from pymonitor.watcher import Watcher


def _uuid(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


def _self_signed_pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-collector")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _pairing(**overrides: Any) -> dict[str, Any]:
    pairing = {
        "version": VERSION,
        "reporterId": _uuid(1),
        "label": "my-watcher",
        "collectorUrl": "https://127.0.0.1:43188/",
        "token": "b" * 64,
        "certificate": _self_signed_pem(),
    }
    pairing.update(overrides)
    return pairing


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / ".local"
    # configuration.py's own read_config/optional_config/save_config reference
    # configuration.data_dir directly; watcher.py imported the name by value
    # (`from .configuration import data_dir`) so it needs its own patch too.
    monkeypatch.setattr(cfg, "data_dir", data_dir)
    monkeypatch.setattr(watcher_mod, "data_dir", data_dir)
    # watcher.py's start()/acquire_role() assume the directory already exists
    # (real entrypoints create it via configuration.initialize()/_protect_data
    # before a Watcher is ever constructed).
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


@pytest.fixture(autouse=True)
def _stub_reporting(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub out everything begin_reporting()/poll() would otherwise touch.

    create_local_observer normally builds a real FamilyMonitor + LocalSource
    against ~/.copilot; these tests only care about watcher.py's own
    identity/pairing/control-server logic, so replace it with a minimal
    fake and never let the ~1.5s polling loop actually run network/SDK code.
    """
    fake_monitor = object()
    fake_source = object()
    monkeypatch.setattr(
        watcher_mod,
        "create_local_observer",
        lambda *a, **k: {"monitor": fake_monitor, "source": fake_source},
    )
    monkeypatch.setattr(watcher_mod, "poll_local", AsyncMock(return_value={
        "result": {"members": [], "gap": False}, "issues": [], "healthy": True, "samples": [],
    }))
    monkeypatch.setattr(watcher_mod.Reporter, "connect", AsyncMock())
    monkeypatch.setattr(watcher_mod.Reporter, "send", AsyncMock(return_value={"healthy": True}))
    monkeypatch.setattr(watcher_mod.Reporter, "disconnect", AsyncMock())


async def _started(tmp_path: Path) -> Watcher:
    w = Watcher()
    await w.start()
    return w


async def test_identity_is_generated_fresh_on_first_start(tmp_path: Path) -> None:
    w = await _started(tmp_path)
    try:
        assert w.identity is not None
        assert w.identity["generation"] == 1
        saved = json.loads((cfg.data_dir / "watcher-identity.json").read_text(encoding="utf-8"))
        assert saved == w.identity
    finally:
        await w.stop()


async def test_identity_persists_installation_id_and_increments_generation(tmp_path: Path) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "watcher-identity.json").write_text(
        json.dumps({"installationId": _uuid(9), "generation": 5, "bootId": _uuid(8)}), encoding="utf-8"
    )
    w = await _started(tmp_path)
    try:
        assert w.identity["installationId"] == _uuid(9)
        assert w.identity["generation"] == 6
        assert w.identity["bootId"] != _uuid(8), "bootId must be freshly generated every boot"
    finally:
        await w.stop()


async def test_health_waits_to_be_paired_when_no_saved_pairing(tmp_path: Path) -> None:
    w = await _started(tmp_path)
    try:
        assert w.health["healthy"] is False
        assert "Waiting to be paired" in w.health["issue"]
    finally:
        await w.stop()


async def test_begin_reporting_runs_automatically_when_pairing_is_saved(tmp_path: Path) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    pairing = _pairing()
    (cfg.data_dir / "watcher.json").write_text(json.dumps(pairing), encoding="utf-8")
    w = await _started(tmp_path)
    try:
        assert w.pairing == pairing
        assert w.reporter is not None, "an existing pairing must start reporting automatically on boot"
        watcher_mod.Reporter.connect.assert_awaited()
    finally:
        await w.stop()


async def test_poll_consumes_and_clears_a_pending_reset_reason(tmp_path: Path) -> None:
    """Regression guard for watcher.py's forced-gap-reason wiring (poll()):
    self.reset is a one-shot flag set by the 'power'/'error' bridge events
    (see tray_native.py's on_power_event/on_bridge_fault handling) and must
    be read into this cycle's forced gap
    reason then cleared, so the NEXT poll cycle does not keep re-forcing a
    gap (which would otherwise keep rebaselining forever)."""
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    (cfg.data_dir / "watcher.json").write_text(json.dumps(_pairing()), encoding="utf-8")
    w = await _started(tmp_path)
    try:
        w.reset = "Windows process observation interrupted; rebaselining"
        await w.poll()
        watcher_mod.poll_local.assert_awaited_with(
            {"source": w.source, "monitor": w.monitor},
            "Windows process observation interrupted; rebaselining",
        )
        assert w.reset is None

        watcher_mod.poll_local.reset_mock()
        await w.poll()
        watcher_mod.poll_local.assert_awaited_with({"source": w.source, "monitor": w.monitor}, None)
    finally:
        await w.stop()


async def test_apply_connection_string_rejects_a_different_reporter(tmp_path: Path) -> None:
    w = await _started(tmp_path)
    try:
        first = _pairing(reporterId=_uuid(1))
        await w.apply_connection_string(encode_connection_string(first))
        assert w.pairing["reporterId"] == _uuid(1)

        other = _pairing(reporterId=_uuid(2))
        with pytest.raises(RuntimeError, match="already paired with a different host"):
            await w.apply_connection_string(encode_connection_string(other))
        assert w.pairing["reporterId"] == _uuid(1), "a rejected re-pair must not overwrite the existing pairing"
    finally:
        await w.stop()


async def test_apply_connection_string_accepts_same_reporter_repaired(tmp_path: Path) -> None:
    w = await _started(tmp_path)
    try:
        pairing = _pairing(reporterId=_uuid(1))
        result = await w.apply_connection_string(encode_connection_string(pairing))
        assert result["label"] == "my-watcher"
        assert result["host"] == "127.0.0.1:43188"

        # Re-pairing with the same reporterId (e.g. a rotated token) must be accepted.
        updated = _pairing(reporterId=_uuid(1), token="c" * 64)
        await w.apply_connection_string(encode_connection_string(updated))
        assert w.pairing["token"] == "c" * 64
    finally:
        await w.stop()


class _ControlClient:
    """Thin helper around TestClient that fixes watcher.port to the test server's port.

    _same_origin's Host check compares against f"127.0.0.1:{self.port}", which
    is only set by start()'s real TCPSite; here the control app is served by
    aiohttp's TestServer on its own ephemeral port instead, so watcher.port is
    patched to match before any request is made.
    """

    def __init__(self, watcher: Watcher, client: TestClient) -> None:
        self.watcher = watcher
        self.client = client

    async def request(self, method: str, path: str, *, host: str | None = None, **kwargs: Any):
        headers = kwargs.pop("headers", {}) or {}
        headers.setdefault("Host", host if host is not None else f"127.0.0.1:{self.watcher.port}")
        return await self.client.request(method, path, headers=headers, **kwargs)


@pytest.fixture
async def control(tmp_path: Path):
    w = await _started(tmp_path)
    server = TestServer(w.app)
    await server.start_server()
    w.port = server.port
    client = TestClient(server)
    await client.start_server()
    try:
        yield w, _ControlClient(w, client)
    finally:
        await client.close()
        await w.stop()


async def test_status_rejects_wrong_host_header(control) -> None:
    w, client = control
    resp = await client.request("GET", "/status", host="example.com")
    assert resp.status == 403


async def test_status_rejects_cross_site_origin(control) -> None:
    w, client = control
    resp = await client.request("GET", "/status", headers={"Sec-Fetch-Site": "cross-site"})
    assert resp.status == 403


async def test_status_returns_health_and_pairing_state(control) -> None:
    w, client = control
    resp = await client.request("GET", "/status")
    assert resp.status == 200
    body = await resp.json()
    assert body["instanceId"] == w.identity["bootId"]
    assert body["paired"] is False
    assert body["reporterId"] is None


async def test_connect_rejects_missing_or_wrong_bearer_token(control) -> None:
    w, client = control
    resp = await client.request("POST", "/connect", headers={"Authorization": "Bearer wrong-token"}, json={"value": ""})
    assert resp.status == 403

    resp2 = await client.request("POST", "/connect", json={"value": ""})
    assert resp2.status == 403


async def test_connect_applies_a_valid_connection_string_with_correct_token(control) -> None:
    w, client = control
    pairing = _pairing(reporterId=_uuid(4))
    value = encode_connection_string(pairing)
    resp = await client.request(
        "POST", "/connect",
        headers={"Authorization": f"Bearer {w.control_token}"},
        json={"value": value},
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    assert body["label"] == "my-watcher"
    assert w.pairing["reporterId"] == _uuid(4)


async def test_connect_reports_decode_failure_as_400(control) -> None:
    w, client = control
    resp = await client.request(
        "POST", "/connect",
        headers={"Authorization": f"Bearer {w.control_token}"},
        json={"value": "not-a-connection-string"},
    )
    assert resp.status == 400
    body = await resp.json()
    assert body["ok"] is False


async def test_stop_rejects_wrong_bearer_token(control) -> None:
    w, client = control
    resp = await client.request("POST", "/stop", headers={"Authorization": "Bearer wrong-token"})
    assert resp.status == 403


async def test_stop_with_correct_token_begins_shutdown(control) -> None:
    w, client = control
    resp = await client.request("POST", "/stop", headers={"Authorization": f"Bearer {w.control_token}"})
    assert resp.status == 200
    body = await resp.json()
    assert body["stopping"] is True
