"""Port of src/reporter.mjs.

`Reporter` is the watcher-side client of the ingestion protocol: it holds
the validated pairing + durable identity, performs the `/v1/connect`
handshake to obtain a lease, and sends `/v1/report` envelopes. All network
I/O goes through `protocol.request()` (cert-pinned HTTPS POST), so this
module is pure sequencing/bookkeeping around that call.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .protocol import HASH_RE, VERSION, ProtocolError, request, validate_pairing, validate_report

__all__ = ["Reporter"]


class Reporter:
    def __init__(self, pairing: dict[str, Any], identity: dict[str, Any]) -> None:
        self.pairing = validate_pairing(pairing)
        self.identity = identity
        self.lease: str | None = None
        self.seq = 0

    async def connect(self) -> None:
        response = await request(self.pairing, "/v1/connect", {
            "version": VERSION,
            "reporterId": self.pairing["reporterId"],
            **self.identity,
        })
        if response.get("version") != VERSION or not (
            isinstance(response.get("lease"), str) and HASH_RE.match(response["lease"])
        ):
            raise RuntimeError("Invalid collector handshake")
        self.lease = response["lease"]
        self.seq = 0

    async def send(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        report = validate_report({
            "version": VERSION,
            "reporterId": self.pairing["reporterId"],
            "lease": self.lease,
            "seq": self.seq + 1,
            "sentAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            **snapshot,
        })
        try:
            try:
                response = await request(self.pairing, "/v1/report", report)
            except ProtocolError as error:
                if error.status != 503:
                    raise
                response = await request(self.pairing, "/v1/report", report)
            if response.get("seq") != report["seq"]:
                raise RuntimeError("Unexpected collector acknowledgement")
            self.seq = report["seq"]
            return response
        except Exception:
            self.lease = None
            raise

    async def disconnect(self) -> None:
        if not self.lease:
            return
        lease = self.lease
        self.lease = None
        await request(self.pairing, "/v1/disconnect", {"lease": lease})
