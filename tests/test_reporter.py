"""Tests for pymonitor.reporter (port of src/reporter.mjs).

No JS test oracle exists for reporter.mjs in isolation (it's only exercised
indirectly via test/controls.test.mjs's spawned worker fixtures, which drive
real sockets end-to-end). These tests instead cover reporter.py's own
sequencing/bookkeeping logic directly, mocking `protocol.request()` so no
real network/TLS is involved -- the wire-level validation it relies on
(validate_report, validate_pairing, HASH_RE) is already covered by
test_protocol.py.
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


@pytest.fixture
def reporter() -> Reporter:
    return Reporter(_pairing(), _identity())


async def test_connect_stores_lease_and_resets_seq(reporter: Reporter, monkeypatch: pytest.MonkeyPatch) -> None:
    lease = "a" * 64
    mock_request = AsyncMock(return_value={"version": proto.VERSION, "lease": lease})
    monkeypatch.setattr("pymonitor.reporter.request", mock_request)

    reporter.seq = 7
    await reporter.connect()

    assert reporter.lease == lease
    assert reporter.seq == 0
    mock_request.assert_awaited_once()
    pairing_arg, route_arg, body_arg = mock_request.await_args.args
    assert route_arg == "/v1/connect"
    assert body_arg["reporterId"] == reporter.pairing["reporterId"]
    assert body_arg["installationId"] == reporter.identity["installationId"]


async def test_connect_rejects_malformed_handshake(reporter: Reporter, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_request = AsyncMock(return_value={"version": proto.VERSION, "lease": "not-a-hash"})
    monkeypatch.setattr("pymonitor.reporter.request", mock_request)
    with pytest.raises(RuntimeError, match="Invalid collector handshake"):
        await reporter.connect()
    assert reporter.lease is None


async def test_send_happy_path_advances_seq(reporter: Reporter, monkeypatch: pytest.MonkeyPatch) -> None:
    reporter.lease = "a" * 64
    reporter.seq = 3
    mock_request = AsyncMock(return_value={"seq": 4})
    monkeypatch.setattr("pymonitor.reporter.request", mock_request)

    response = await reporter.send(_snapshot())

    assert response == {"seq": 4}
    assert reporter.seq == 4
    assert reporter.lease == "a" * 64  # lease untouched on success
    sent_body = mock_request.await_args.args[2]
    assert sent_body["seq"] == 4
    assert sent_body["lease"] == "a" * 64


async def test_send_retries_once_on_503(reporter: Reporter, monkeypatch: pytest.MonkeyPatch) -> None:
    reporter.lease = "a" * 64
    reporter.seq = 0
    error_503 = proto.ProtocolError("Collector busy", 503)
    mock_request = AsyncMock(side_effect=[error_503, {"seq": 1}])
    monkeypatch.setattr("pymonitor.reporter.request", mock_request)

    response = await reporter.send(_snapshot())

    assert response == {"seq": 1}
    assert reporter.seq == 1
    assert mock_request.await_count == 2


async def test_send_does_not_retry_non_503_errors(reporter: Reporter, monkeypatch: pytest.MonkeyPatch) -> None:
    reporter.lease = "a" * 64
    error_409 = proto.ProtocolError("Lease conflict", 409)
    mock_request = AsyncMock(side_effect=error_409)
    monkeypatch.setattr("pymonitor.reporter.request", mock_request)

    with pytest.raises(proto.ProtocolError):
        await reporter.send(_snapshot())

    assert mock_request.await_count == 1
    assert reporter.lease is None, "lease must be dropped on any send failure"


async def test_send_drops_lease_on_seq_mismatch(reporter: Reporter, monkeypatch: pytest.MonkeyPatch) -> None:
    reporter.lease = "a" * 64
    reporter.seq = 0
    mock_request = AsyncMock(return_value={"seq": 999})
    monkeypatch.setattr("pymonitor.reporter.request", mock_request)

    with pytest.raises(RuntimeError, match="Unexpected collector acknowledgement"):
        await reporter.send(_snapshot())

    assert reporter.lease is None
    assert reporter.seq == 0, "seq must not advance on a rejected acknowledgement"


async def test_send_without_lease_fails_validation(reporter: Reporter, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_request = AsyncMock()
    monkeypatch.setattr("pymonitor.reporter.request", mock_request)

    with pytest.raises(proto.ProtocolError):
        await reporter.send(_snapshot())

    mock_request.assert_not_awaited()
    assert reporter.lease is None


async def test_disconnect_is_noop_without_lease(reporter: Reporter, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_request = AsyncMock()
    monkeypatch.setattr("pymonitor.reporter.request", mock_request)
    await reporter.disconnect()
    mock_request.assert_not_awaited()


async def test_disconnect_sends_lease_and_clears_it(reporter: Reporter, monkeypatch: pytest.MonkeyPatch) -> None:
    reporter.lease = "a" * 64
    mock_request = AsyncMock(return_value={})
    monkeypatch.setattr("pymonitor.reporter.request", mock_request)

    await reporter.disconnect()

    mock_request.assert_awaited_once()
    args = mock_request.await_args.args
    assert args[1] == "/v1/disconnect"
    assert args[2]["lease"] == "a" * 64
    assert reporter.lease is None
