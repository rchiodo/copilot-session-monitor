"""Tests for the Phase 2 wire-protocol additions in pymonitor.protocol.

Covers envelope validation (validate_connect/validate_report), the bearer-auth check, private-address classification, and the csm1: connection-string
codec round-trip -- the pure, network-independent pieces of protocol.py --
plus real (non-mocked) TLS integration tests for request()'s cert-pin check
at the bottom of the file.
"""
from __future__ import annotations

import datetime
import hashlib
import ipaddress
import ssl

import pytest
from aiohttp import web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from pymonitor import protocol as proto


def _uuid(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


def _self_signed_pem(*, days_valid: int = 1, not_before_offset_days: int = 0) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-collector")])
    now = datetime.datetime.now(datetime.timezone.utc)
    not_before = now + datetime.timedelta(days=not_before_offset_days)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_before + datetime.timedelta(days=days_valid))
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _valid_connect() -> dict:
    return {
        "version": proto.VERSION,
        "reporterId": _uuid(1),
        "installationId": _uuid(2),
        "bootId": _uuid(3),
        "generation": 1,
    }


def test_validate_connect_accepts_well_formed_handshake() -> None:
    value = _valid_connect()
    assert proto.validate_connect(value) == value


@pytest.mark.parametrize(
    "mutate",
    [
        lambda v: v.update(version=2),
        lambda v: v.update(reporterId="not-a-uuid"),
        lambda v: v.update(generation=0),
        lambda v: v.update(generation=True),  # bool must not satisfy the integer check
        lambda v: v.pop("bootId"),
        lambda v: v.update(extra="nope"),
    ],
)
def test_validate_connect_rejects_malformed_handshake(mutate) -> None:
    value = _valid_connect()
    mutate(value)
    with pytest.raises(proto.ProtocolError):
        proto.validate_connect(value)


def _valid_member(**overrides) -> dict:
    row = {
        "id": "sess-1",
        "parentId": None,
        "runId": None,
        "title": "My session",
        "source": "Copilot desktop",
        "state": "working",
        "detail": "working on it",
        "activity": "editing",
        "hierarchyIssue": None,
        "contextOnly": False,
        "completionTracked": True,
        "firstObservedAt": "2024-01-01T00:00:00.000Z",
        "startedAt": "2024-01-01T00:00:00.000Z",
        "lastEventAt": "2024-01-01T00:00:01.000Z",
        "lastResponseAt": None,
        "finishedAt": None,
        "lastAlert": None,
    }
    row.update(overrides)
    return row


def _valid_report(**overrides) -> dict:
    report = {
        "version": proto.VERSION,
        "reporterId": _uuid(1),
        "lease": "a" * 64,
        "seq": 1,
        "sentAt": "2024-01-01T00:00:02.000Z",
        "healthy": True,
        "issues": [],
        "members": [_valid_member()],
        "relatives": [],
        "notices": [],
    }
    report.update(overrides)
    return report


def test_validate_report_accepts_well_formed_report() -> None:
    value = _valid_report()
    assert proto.validate_report(value) == value


def test_validate_report_rejects_duplicate_member_ids() -> None:
    value = _valid_report(members=[_valid_member(), _valid_member()])
    with pytest.raises(proto.ProtocolError):
        proto.validate_report(value)


def test_validate_report_rejects_finished_state_without_timestamp() -> None:
    value = _valid_report(members=[_valid_member(state="finished", finishedAt=None)])
    with pytest.raises(proto.ProtocolError):
        proto.validate_report(value)


def test_validate_report_rejects_completion_tracked_on_terminal_state() -> None:
    value = _valid_report(members=[_valid_member(state="finished", finishedAt="2024-01-01T00:00:03.000Z",
                                                   completionTracked=True)])
    with pytest.raises(proto.ProtocolError):
        proto.validate_report(value)


def test_validate_report_rejects_too_many_members() -> None:
    value = _valid_report(members=[_valid_member(id=f"sess-{i}") for i in range(5001)])
    with pytest.raises(proto.ProtocolError):
        proto.validate_report(value)


def test_validate_report_rejects_bad_lease_hash() -> None:
    value = _valid_report(lease="not-a-hash")
    with pytest.raises(proto.ProtocolError):
        proto.validate_report(value)


def test_authorized_accepts_matching_bearer_token() -> None:
    secret = "shh"
    expected_hash = proto.digest(secret)
    assert proto.authorized(f"Bearer {secret}", expected_hash) is True


def test_authorized_rejects_wrong_token() -> None:
    expected_hash = proto.digest("shh")
    assert proto.authorized("Bearer wrong", expected_hash) is False


def test_authorized_rejects_missing_bearer_prefix() -> None:
    expected_hash = proto.digest("shh")
    assert proto.authorized("shh", expected_hash) is False


def test_authorized_rejects_malformed_expected_hash() -> None:
    assert proto.authorized("Bearer shh", "not-a-hash") is False


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "::1", "10.1.2.3", "172.16.0.1", "172.31.255.255", "192.168.1.1",
     "169.254.1.1", "100.64.0.1", "fd00::1", "fe80::1"],
)
def test_private_address_accepts_private_and_loopback(address: str) -> None:
    assert proto.private_address(address) is True


@pytest.mark.parametrize("address", ["8.8.8.8", "172.32.0.1", "1.1.1.1", "2001:4860:4860::8888", "not-an-ip"])
def test_private_address_rejects_public_addresses(address: str) -> None:
    assert proto.private_address(address) is False


def _valid_pairing(**overrides) -> dict:
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


def test_connection_string_round_trips() -> None:
    pairing = _valid_pairing()
    encoded = proto.encode_connection_string(pairing)
    assert encoded.startswith(proto.CONNECTION_PREFIX)
    assert proto.decode_connection_string(encoded) == pairing


def test_decode_connection_string_rejects_bad_prefix() -> None:
    with pytest.raises(proto.ProtocolError):
        proto.decode_connection_string("not-csm1:abc")


def test_decode_connection_string_rejects_malformed_payload() -> None:
    with pytest.raises(proto.ProtocolError):
        proto.decode_connection_string(proto.CONNECTION_PREFIX + "!!!not-base64!!!")


def test_validate_pairing_rejects_non_private_host() -> None:
    pairing = _valid_pairing(collectorUrl="https://8.8.8.8:43188/")
    with pytest.raises(proto.ProtocolError):
        proto.validate_pairing(pairing)


def test_validate_pairing_rejects_http_scheme() -> None:
    pairing = _valid_pairing(collectorUrl="http://127.0.0.1:43188/")
    with pytest.raises(proto.ProtocolError):
        proto.validate_pairing(pairing)


def test_validate_pairing_rejects_low_port() -> None:
    pairing = _valid_pairing(collectorUrl="https://127.0.0.1:80/")
    with pytest.raises(proto.ProtocolError):
        proto.validate_pairing(pairing)


def test_validate_pairing_rejects_expired_certificate() -> None:
    expired_pem = _self_signed_pem(days_valid=1, not_before_offset_days=-30)
    pairing = _valid_pairing(certificate=expired_pem)
    with pytest.raises(proto.ProtocolError):
        proto.validate_pairing(pairing)


def test_validate_pairing_rejects_multiple_certificates() -> None:
    pairing = _valid_pairing(certificate=_self_signed_pem() + _self_signed_pem())
    with pytest.raises(proto.ProtocolError):
        proto.validate_pairing(pairing)


# -- real (non-mocked) TLS integration tests for request()'s cert-pin check --
#
# The unit tests above exercise protocol.py's pure validation logic; nothing
# in this file previously drove request() against an actual TLS connection.
# That gap is exactly how the connection-release race (response.connection
# going None before the post-hoc cert check ran, for small fully-buffered
# JSON bodies -- see _PinCapturingConnector in protocol.py) shipped
# undetected: every existing request()-adjacent test mocks it out. These
# tests spin up a real aiohttp HTTPS listener instead.


def _self_signed_cert_and_key() -> tuple[str, bytes]:
    """Self-signed cert (PEM str) + matching private key (PEM bytes) with a
    127.0.0.1 SAN, suitable for a real loopback TLS handshake."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-collector")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return cert_pem, key_pem


async def _echo_ok(_request: web.Request) -> web.Response:
    # Small, fully-buffered JSON body -- the exact shape that triggers aiohttp's
    # synchronous connection-release-during-start() race this fix addresses.
    return web.json_response({"ok": True})


async def _start_tls_echo_server(tmp_path, cert_pem: str, key_pem: bytes) -> tuple[web.AppRunner, int]:
    cert_path = tmp_path / "server-cert.pem"
    key_path = tmp_path / "server-key.pem"
    cert_path.write_text(cert_pem, encoding="ascii")
    key_path.write_bytes(key_pem)
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(str(cert_path), str(key_path))
    app = web.Application()
    app.router.add_post("/v1/report", _echo_ok)
    runner = web.AppRunner(app, shutdown_timeout=2.0)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=ssl_context)
    await site.start()
    port = runner.addresses[0][1]
    return runner, port


def _pairing_for(cert_pem: str, port: int) -> dict:
    return {
        "version": proto.VERSION,
        "reporterId": _uuid(1),
        "label": "test",
        "collectorUrl": f"https://127.0.0.1:{port}/",
        "token": hashlib.sha256(b"shh").hexdigest(),
        "certificate": cert_pem,
    }


async def test_request_succeeds_against_real_tls_server_with_matching_pin(tmp_path) -> None:
    """Regression test for the connection-release race: a real (non-mocked) TLS
    server returning a small, fully-buffered JSON body must not trip a false
    pin mismatch (the exact bug this fix addresses)."""
    cert_pem, key_pem = _self_signed_cert_and_key()
    runner, port = await _start_tls_echo_server(tmp_path, cert_pem, key_pem)
    try:
        result = await proto.request(_pairing_for(cert_pem, port), "v1/report", {"kind": "finished"})
        assert result == {"ok": True}
    finally:
        await runner.cleanup()


async def test_request_rejects_real_tls_server_with_different_cert(tmp_path) -> None:
    """A real TLS server presenting a cert other than the pinned one must still be
    rejected -- proving the connector-based fix didn't neuter the pin check."""
    server_cert_pem, key_pem = _self_signed_cert_and_key()
    other_cert_pem, _unused_key = _self_signed_cert_and_key()
    runner, port = await _start_tls_echo_server(tmp_path, server_cert_pem, key_pem)
    try:
        with pytest.raises(proto.ProtocolError):
            await proto.request(_pairing_for(other_cert_pem, port), "v1/report", {"kind": "finished"})
    finally:
        await runner.cleanup()
