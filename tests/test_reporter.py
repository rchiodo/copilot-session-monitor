"""Tests for pymonitor.reporter (port of src/reporter.mjs).

No JS test oracle exists for reporter.mjs in isolation (it's only exercised
indirectly via test/controls.test.mjs's spawned worker fixtures, which drive
real sockets end-to-end). These tests instead cover reporter.py's own
sequencing/bookkeeping logic directly, replacing `protocol.ReporterConnection`
with `_FakeConnection` so no real network/TLS is involved -- the wire-level
validation it relies on (validate_report, validate_pairing, HASH_RE) is
already covered by test_protocol.py, and ReporterConnection itself is
covered directly by test_protocol.py's WebSocket tests.
"""
from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from pymonitor import protocol as proto
from pymonitor.reporter import Reporter


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
        "version": proto.VERSION,
        "reporterId": _uuid(1),
        "label": "my-watcher",
        "collectorUrl": "https://127.0.0.1:43188/",
        "token": "b" * 64,
        "certificate": _self_signed_pem(),
    }
    pairing.update(overrides)
    return pairing


def _identity() -> dict[str, Any]:
    return {"installationId": _uuid(2), "bootId": _uuid(3), "generation": 1}


def _snapshot() -> dict[str, Any]:
    return {"healthy": True, "issues": [], "members": [], "relatives": [], "notices": []}


class _FakeConnection:
    """Stand-in for protocol.ReporterConnection: no real socket, just an
    AsyncMock to configure per-test plus a `closed` flag so tests can assert
    disconnect() actually tears the (fake) connection down."""

    def __init__(self, pairing: dict[str, Any]) -> None:
        self.pairing = pairing
        self.request = AsyncMock()
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def reporter(monkeypatch: pytest.MonkeyPatch) -> Reporter:
    monkeypatch.setattr("pymonitor.reporter.ReporterConnection", _FakeConnection)
    return Reporter(_pairing(), _identity())


async def test_connect_stores_lease_and_resets_seq(reporter: Reporter) -> None:
    lease = "a" * 64
    reporter._conn.request.return_value = {"version": proto.VERSION, "lease": lease}

    reporter.seq = 7
    await reporter.connect()

    assert reporter.lease == lease
    assert reporter.seq == 0
    reporter._conn.request.assert_awaited_once()
    route_arg, body_arg = reporter._conn.request.await_args.args
    assert route_arg == "/v1/connect"
    assert body_arg["reporterId"] == reporter.pairing["reporterId"]
    assert body_arg["installationId"] == reporter.identity["installationId"]


async def test_connect_rejects_malformed_handshake(reporter: Reporter) -> None:
    reporter._conn.request.return_value = {"version": proto.VERSION, "lease": "not-a-hash"}
    with pytest.raises(RuntimeError, match="Invalid collector handshake"):
        await reporter.connect()
    assert reporter.lease is None


async def test_send_happy_path_advances_seq(reporter: Reporter) -> None:
    reporter.lease = "a" * 64
    reporter.seq = 3
    reporter._conn.request.return_value = {"seq": 4}

    response = await reporter.send(_snapshot())

    assert response == {"seq": 4}
    assert reporter.seq == 4
    assert reporter.lease == "a" * 64  # lease untouched on success
    sent_body = reporter._conn.request.await_args.args[1]
    assert sent_body["seq"] == 4
    assert sent_body["lease"] == "a" * 64


async def test_send_retries_once_on_503(reporter: Reporter) -> None:
    reporter.lease = "a" * 64
    reporter.seq = 0
    error_503 = proto.ProtocolError("Collector busy", 503)
    reporter._conn.request.side_effect = [error_503, {"seq": 1}]

    response = await reporter.send(_snapshot())

    assert response == {"seq": 1}
    assert reporter.seq == 1
    assert reporter._conn.request.await_count == 2


async def test_send_retries_once_on_409_via_reconnect(reporter: Reporter) -> None:
    reporter.lease = "a" * 64
    reporter.seq = 3
    new_lease = "b" * 64
    error_409 = proto.ProtocolError("Lease expired; reconnect and baseline", 409)
    reporter._conn.request.side_effect = [
        error_409,
        {"version": proto.VERSION, "lease": new_lease},
        {"seq": 1},
    ]

    response = await reporter.send(_snapshot())

    assert response == {"seq": 1}
    assert reporter.lease == new_lease
    assert reporter.seq == 1
    assert reporter._conn.request.await_count == 3
    routes = [call.args[0] for call in reporter._conn.request.await_args_list]
    assert routes == ["/v1/report", "/v1/connect", "/v1/report"]
    final_body = reporter._conn.request.await_args_list[-1].args[1]
    assert final_body["lease"] == new_lease
    assert final_body["seq"] == 1


async def test_send_409_retry_failure_propagates(reporter: Reporter) -> None:
    reporter.lease = "a" * 64
    error_report = proto.ProtocolError("Lease expired; reconnect and baseline", 409)
    error_connect = proto.ProtocolError("Another watcher instance holds this identity", 409)
    reporter._conn.request.side_effect = [error_report, error_connect]

    with pytest.raises(proto.ProtocolError, match="Another watcher instance"):
        await reporter.send(_snapshot())

    assert reporter._conn.request.await_count == 2
    assert reporter.lease is None, "lease must be dropped on any send failure"


async def test_send_does_not_retry_non_retryable_errors(reporter: Reporter) -> None:
    reporter.lease = "a" * 64
    error_403 = proto.ProtocolError("Reporter revoked", 403)
    reporter._conn.request.side_effect = error_403

    with pytest.raises(proto.ProtocolError):
        await reporter.send(_snapshot())

    assert reporter._conn.request.await_count == 1
    assert reporter.lease is None, "lease must be dropped on any send failure"


async def test_send_drops_lease_on_seq_mismatch(reporter: Reporter) -> None:
    reporter.lease = "a" * 64
    reporter.seq = 0
    reporter._conn.request.return_value = {"seq": 999}

    with pytest.raises(RuntimeError, match="Unexpected collector acknowledgement"):
        await reporter.send(_snapshot())

    assert reporter.lease is None
    assert reporter.seq == 0, "seq must not advance on a rejected acknowledgement"


async def test_send_without_lease_fails_validation(reporter: Reporter) -> None:
    with pytest.raises(proto.ProtocolError):
        await reporter.send(_snapshot())

    reporter._conn.request.assert_not_awaited()
    assert reporter.lease is None


async def test_disconnect_is_noop_without_lease(reporter: Reporter) -> None:
    await reporter.disconnect()
    reporter._conn.request.assert_not_awaited()
    assert reporter._conn.closed is False


async def test_disconnect_sends_lease_and_clears_it(reporter: Reporter) -> None:
    reporter.lease = "a" * 64
    reporter._conn.request.return_value = {}

    await reporter.disconnect()

    reporter._conn.request.assert_awaited_once()
    args = reporter._conn.request.await_args.args
    assert args[0] == "/v1/disconnect"
    assert args[1]["lease"] == "a" * 64
    assert reporter.lease is None
    assert reporter._conn.closed is True
