"""Port of src/protocol.mjs.

Phase 1 ported only the pure, network-independent member/relative field
projection (`metadata`/`observed_metadata`). Phase 2 adds the rest of the
wire protocol: envelope validation (`validate_connect`/`validate_report`),
the Bearer-token auth check, the private-address guard, the `csm1:`
pairing/connection-string codec, and the cert-pinned HTTPS client
(`request`) used by reporter.py. `read_json` is the server-side counterpart,
written against an aiohttp ``web.Request`` (see docs/porting-notes.md for
why aiohttp was chosen over stdlib http.server for Phase 2).

`digest()` (sha256 hex) already lives in engine.py (used internally there
by Ledger/SessionStore); this module re-exports it rather than duplicating
it, documenting the module-boundary difference from the original single
protocol.mjs file.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import re
import ssl as ssl_module
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

import aiohttp
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from .engine import digest

if TYPE_CHECKING:  # pragma: no cover
    from aiohttp import web

__all__ = [
    "VERSION", "HEARTBEAT_MS", "MAX_BYTES", "UUID_RE", "HASH_RE", "CONNECTION_PREFIX",
    "digest", "metadata", "observed_metadata", "ProtocolError", "fail",
    "validate_connect", "validate_report", "read_json", "authorized", "private_address",
    "validate_pairing", "encode_connection_string", "decode_connection_string", "request",
]

VERSION = 1
HEARTBEAT_MS = 15000
MAX_BYTES = 4 * 1024 * 1024
UUID_RE = re.compile(r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$", re.IGNORECASE)
HASH_RE = re.compile(r"^[a-f0-9]{64}$")
CONNECTION_PREFIX = "csm1:"

_MEMBER_FIELDS = [
    "id", "title", "source", "state", "detail", "activity", "runId", "firstObservedAt",
    "startedAt", "lastEventAt", "lastResponseAt", "finishedAt", "parentId", "hierarchyIssue",
    "contextOnly", "lastAlert", "completionTracked",
]
_RELATIVE_FIELDS = ["id", "title", "parentId", "aliasOf", "hierarchyIssue", "detail"]
_STATES = ["working", "finished", "waiting", "error", "unknown", "idle"]
_KINDS = ["finished", "waiting", "error", "warning"]


class ProtocolError(Exception):
    """Mirrors the JS `Object.assign(new Error(message), { status })` pattern."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def fail(message: str, status: int = 400) -> ProtocolError:
    return ProtocolError(message, status)


def _is_int(value: Any) -> bool:
    # Python bool is an int subclass; JS `typeof true !== 'number'` rejects
    # booleans implicitly wherever Number.isSafeInteger()/typeof checks are used.
    return isinstance(value, int) and not isinstance(value, bool)


def _object(value: Any, keys: list[str]) -> None:
    if not isinstance(value, dict) or any(key not in keys for key in value.keys()):
        raise fail("Unexpected metadata fields")


def _text(value: Any, max_len: int = 512, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or not value or len(value) > max_len or re.search(r"[\x00-\x1f]", value):
        raise fail("Invalid metadata string")


def _id(value: Any, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    _text(value, 128)
    if not re.match(r"^[a-zA-Z0-9_.:-]+$", value):
        raise fail("Invalid session identity")


def _date(value: Any, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if not isinstance(value, str) or not re.match(r"^\d{4}-\d\d-\d\dT", value) or len(value) > 35:
        raise fail("Invalid metadata timestamp")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise fail("Invalid metadata timestamp") from error


def _pick(value: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    return {key: value.get(key, None) for key in fields}


def metadata(snapshot: dict[str, Any]) -> dict[str, Any]:
    members = []
    for row in snapshot["members"]:
        picked = _pick(row, _MEMBER_FIELDS)
        tracked = row.get("completionTracked")
        picked["completionTracked"] = tracked if tracked is not None else row.get("state") == "working"
        members.append(picked)
    relatives = [_pick(row, _RELATIVE_FIELDS) for row in snapshot.get("relatives") or []]
    return {"members": members, "relatives": relatives}


def observed_metadata(
    snapshot: dict[str, Any],
    samples: list[dict[str, Any]],
    tracked: dict[str, Any] | None = None,
) -> dict[str, Any]:
    tracked = tracked or {}
    current = {sample["id"]: sample for sample in samples}
    members = []
    for row in snapshot["members"]:
        sample = current.get(row["id"])
        # A finish already confirmed by the engine's own event-tailing state machine is durable
        # (engine.py deliberately keeps 'finished' rows even through a missing/dead sample or a
        # full observation gap). The owning process exiting shortly after it finishes is the normal
        # lifecycle for every completed run, so mere absence of fresh live evidence must not demote
        # it back to unconfirmed. Only demote when the current sample affirmatively contradicts the
        # finish: the owning session is unreadable/lost, completion is ambiguous relative to a newer
        # owner, or the underlying event log was rotated/replaced.
        if row["state"] == "finished" and sample and (
            sample.get("readError")
            or sample.get("completionUnconfirmed")
            or (sample.get("events") or {}).get("closed")
            or (sample.get("events") or {}).get("replaced")
        ):
            members.append({
                **row,
                "completionTracked": False,
                "state": "unknown",
                "finishedAt": None,
                "detail": "Finished run owner/evidence is unavailable; current status unconfirmed",
            })
        else:
            members.append({**row, "completionTracked": row["id"] in tracked})
    return metadata({**snapshot, "members": members})


def validate_connect(value: dict[str, Any]) -> dict[str, Any]:
    _object(value, ["version", "reporterId", "installationId", "bootId", "generation"])
    if (
        value.get("version") != VERSION
        or not all(isinstance(value.get(key), str) and UUID_RE.match(value[key])
                   for key in ("reporterId", "installationId", "bootId"))
        or not _is_int(value.get("generation")) or value["generation"] < 1
    ):
        raise fail("Invalid watcher handshake")
    return value


def validate_report(value: dict[str, Any]) -> dict[str, Any]:
    _object(value, ["version", "reporterId", "lease", "seq", "sentAt", "healthy", "issues",
                     "members", "relatives", "notices"])
    if (
        value.get("version") != VERSION
        or not (isinstance(value.get("reporterId"), str) and UUID_RE.match(value["reporterId"]))
        or not (isinstance(value.get("lease"), str) and HASH_RE.match(value["lease"]))
        or not _is_int(value.get("seq")) or value["seq"] < 1
        or not isinstance(value.get("healthy"), bool)
    ):
        raise fail("Invalid report envelope")
    _date(value["sentAt"])
    for key in ("members", "relatives", "notices", "issues"):
        limit = 100 if key == "issues" else 5000
        if not isinstance(value.get(key), list) or len(value[key]) > limit:
            raise fail("Metadata limit exceeded")
    for issue in value["issues"]:
        _text(issue, 1024)
    for row in value["members"]:
        _object(row, _MEMBER_FIELDS)
        _id(row.get("id"))
        _id(row.get("parentId"), True)
        _id(row.get("runId"), True)
        _text(row.get("title"))
        _text(row.get("detail"), 1024)
        _text(row.get("activity"))
        _text(row.get("hierarchyIssue"), 1024, True)
        if (
            row.get("source") not in ("Copilot desktop", "CLI (activity only)")
            or row.get("state") not in _STATES
            or not isinstance(row.get("contextOnly"), bool)
            or not isinstance(row.get("completionTracked"), bool)
            or (row["completionTracked"] and row["state"] not in ("working", "waiting", "unknown"))
        ):
            raise fail("Invalid member status")
        for key in ("firstObservedAt", "startedAt", "lastEventAt", "lastResponseAt", "finishedAt"):
            _date(row.get(key), key != "firstObservedAt")
        if (row["state"] == "finished") != (row.get("finishedAt") is not None):
            raise fail("Invalid completion timestamp")
        if row.get("lastAlert") is not None:
            alert = row["lastAlert"]
            _object(alert, ["sessionId", "key", "kind", "message", "at"])
            if alert.get("sessionId") != row["id"] or alert.get("kind") not in _KINDS:
                raise fail("Invalid parent alert provenance")
            _text(alert.get("key"), 2048)
            _text(alert.get("message"), 1024)
            _date(alert.get("at"))
    for row in value["relatives"]:
        _object(row, _RELATIVE_FIELDS)
        _id(row.get("id"))
        _id(row.get("parentId"), True)
        _id(row.get("aliasOf"), True)
        _text(row.get("title"))
        _text(row.get("detail"), 1024, True)
        _text(row.get("hierarchyIssue"), 1024, True)
    for rows in (value["members"], value["relatives"]):
        if len({row["id"] for row in rows}) != len(rows):
            raise fail("Duplicate member identity")
    for notice in value["notices"]:
        _object(notice, ["key", "familyId", "kind"])
        _id(notice.get("familyId"))
        if not (isinstance(notice.get("key"), str) and HASH_RE.match(notice["key"])) or notice.get("kind") not in _KINDS:
            raise fail("Invalid monitor notice")
    return value


async def read_json(request_: "web.Request") -> Any:
    """Server-side counterpart of request()'s body; reads an aiohttp request."""
    if request_.headers.get("Content-Type") != "application/json" or request_.headers.get("Content-Encoding"):
        raise fail("Uncompressed application/json required", 415)
    content_length = request_.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_BYTES:
                raise fail("Report too large", 413)
        except ValueError:
            pass
    size = 0
    chunks: list[bytes] = []
    async for chunk in request_.content.iter_any():
        size += len(chunk)
        if size > MAX_BYTES:
            raise fail("Report too large", 413)
        chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks).decode("utf-8"))
    except Exception as error:
        raise fail("Invalid report JSON") from error


def authorized(secret: Any, expected_hash: Any) -> bool:
    if not isinstance(secret, str) or not secret.startswith("Bearer ") or not (
        isinstance(expected_hash, str) and HASH_RE.match(expected_hash)
    ):
        return False
    try:
        return hmac.compare_digest(bytes.fromhex(digest(secret[7:])), bytes.fromhex(expected_hash))
    except ValueError:
        return False


def private_address(value: Any) -> bool:
    if value == "::1":
        return True
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        return bool(re.match(r"^(fc|fd|fe[89ab])", str(value), re.IGNORECASE))
    octets = ip.packed
    a, b = octets[0], octets[1]
    return bool(
        a == 127 or a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)
        or (a == 169 and b == 254) or (a == 100 and 64 <= b <= 127)
    )


def _cert_not_after(pem: str) -> datetime:
    cert = x509.load_pem_x509_certificate(pem.encode("utf-8"))
    if hasattr(cert, "not_valid_after_utc"):
        return cert.not_valid_after_utc
    return cert.not_valid_after.replace(tzinfo=timezone.utc)


def _cert_der(pem: str) -> bytes:
    cert = x509.load_pem_x509_certificate(pem.encode("utf-8"))
    return cert.public_bytes(serialization.Encoding.DER)


def _der_fingerprint_sha256(der: bytes) -> str:
    """Short hex SHA-256 fingerprint of a DER certificate, for pin-mismatch diagnostics."""
    return hashlib.sha256(der).hexdigest()[:16]


def validate_pairing(value: dict[str, Any]) -> dict[str, Any]:
    _object(value, ["version", "reporterId", "label", "collectorUrl", "token", "certificate"])
    if (
        value.get("version") != VERSION
        or not (isinstance(value.get("reporterId"), str) and UUID_RE.match(value["reporterId"]))
        or not (isinstance(value.get("token"), str) and HASH_RE.match(value["token"]))
    ):
        raise fail("Invalid pairing")
    _text(value.get("label"), 80)
    try:
        parsed = urlsplit(value["collectorUrl"])
    except Exception as error:
        raise fail("Pairing requires an HTTPS private/loopback IP and explicit port") from error
    hostname = parsed.hostname or ""
    # JS's URL class normalizes an absent path to "/" for special schemes (http/https);
    # Python's urlsplit leaves it as "" instead, so normalize here to match.
    pathname = parsed.path or "/"
    if (
        parsed.scheme != "https" or parsed.username or parsed.password or parsed.query or parsed.fragment
        or pathname != "/" or not private_address(hostname) or not parsed.port or parsed.port < 1024
    ):
        raise fail("Pairing requires an HTTPS private/loopback IP and explicit port")
    certificate = value.get("certificate")
    if (
        not isinstance(certificate, str) or len(certificate) > 16384
        or certificate.count("-----BEGIN CERTIFICATE-----") != 1
    ):
        raise fail("Exactly one collector certificate is required")
    try:
        not_after = _cert_not_after(certificate)
    except Exception as error:
        raise fail("Exactly one collector certificate is required") from error
    if not_after <= datetime.now(timezone.utc):
        raise fail("Pairing certificate expired")
    return value


def encode_connection_string(pairing: dict[str, Any]) -> str:
    validate_pairing(pairing)
    encoded = base64.urlsafe_b64encode(json.dumps(pairing).encode("utf-8")).decode("ascii").rstrip("=")
    return CONNECTION_PREFIX + encoded


def decode_connection_string(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        raise fail("Connection string is required")
    trimmed = value.strip()
    if not trimmed.startswith(CONNECTION_PREFIX):
        raise fail("Unrecognized connection string format")
    if len(trimmed) > 32768:
        raise fail("Connection string is too large")
    payload = trimmed[len(CONNECTION_PREFIX):]
    padded = payload + "=" * (-len(payload) % 4)
    try:
        parsed = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except Exception as error:
        raise fail("Malformed connection string") from error
    return validate_pairing(parsed)


class _PinCapturingConnector(aiohttp.TCPConnector):
    """TCPConnector that stashes the peer cert as the TLS connection is made.

    aiohttp releases a response's connection (response.connection -> None)
    as soon as its body is fully buffered, which for small JSON replies can
    happen before start() even returns to request() below -- so reading the
    peer certificate off response.connection afterwards is a race that loses
    in practice (see the bug this fixed: the pin check reporting "server
    presented none" against a server that really did present the pinned
    cert). Capturing it here, at connection-creation time, is race-free: one
    of these connectors is used for exactly one request (request() below
    creates a fresh session/connector per call), so there's no risk of a
    later connection's cert overwriting an earlier one before it's read.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.captured_peer_der: bytes | None = None

    async def _wrap_create_connection(self, *args: Any, **kwargs: Any) -> Any:
        transport, protocol = await super()._wrap_create_connection(*args, **kwargs)
        ssl_object = transport.get_extra_info("ssl_object")
        if ssl_object is not None:
            self.captured_peer_der = ssl_object.getpeercert(binary_form=True)
        return transport, protocol


async def _read_capped(content: "aiohttp.StreamReader", cap: int, message: str) -> bytes:
    size = 0
    chunks: list[bytes] = []
    async for chunk in content.iter_any():
        size += len(chunk)
        if size > cap:
            raise fail(message)
        chunks.append(chunk)
    return b"".join(chunks)


async def request(pairing: dict[str, Any], route: str, body: dict[str, Any]) -> Any:
    """Cert-pinned HTTPS POST used by reporter.py to talk to the collector.

    Mirrors protocol.mjs's `request()`: builds an SSL context trusting only
    the pinned self-signed collector certificate (standard hostname
    verification via check_hostname plus an explicit raw-DER pin check,
    matching the belt-and-braces checkServerIdentity override in the JS
    original), POSTs the JSON body with the reporter's bearer token, and
    caps/validates the response.
    """
    data = json.dumps(body)
    if len(data.encode("utf-8")) > MAX_BYTES:
        raise fail("Report too large", 413)
    try:
        ssl_context = ssl_module.create_default_context(cadata=pairing["certificate"])
        expected_der = _cert_der(pairing["certificate"])
    except Exception as error:
        raise fail(f"Collector transport unavailable ({type(error).__name__})", 503) from error
    url = urljoin(pairing["collectorUrl"], route)
    headers = {
        "Content-Type": "application/json",
        "X-Monitor-Reporter": pairing["reporterId"],
        "Authorization": f"Bearer {pairing['token']}",
    }
    timeout = aiohttp.ClientTimeout(total=4)
    connector = _PinCapturingConnector(ssl=ssl_context)
    try:
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            async with session.post(url, data=data.encode("utf-8"), headers=headers, ssl=ssl_context) as response:
                peer_der = connector.captured_peer_der
                if peer_der is None or peer_der != expected_der:
                    expected_fp = _der_fingerprint_sha256(expected_der)
                    peer_fp = _der_fingerprint_sha256(peer_der) if peer_der is not None else "none"
                    raise fail(
                        f"Collector certificate pin mismatch (pinned {expected_fp}, server presented {peer_fp})"
                    )
                content = await _read_capped(response.content, 65536, "Collector response too large")
                if response.status != 200:
                    raise fail(f"Collector rejected request ({response.status})", response.status)
                try:
                    return json.loads(content.decode("utf-8"))
                except Exception as error:
                    raise fail("Invalid collector response") from error
    except ProtocolError:
        raise
    except Exception as error:
        raise fail(f"Collector transport unavailable ({type(error).__name__})", 503) from error
