"""Port of src/reporter.mjs.

`Reporter` is the watcher-side client of the ingestion protocol: it holds
the validated pairing + durable identity, performs the `/v1/connect`
handshake to obtain a lease, and sends `/v1/report` envelopes. All network
I/O goes through a single `protocol.ReporterConnection` held for the
lifetime of this Reporter (a persistent, cert-pinned WebSocket), so this
module is pure sequencing/bookkeeping around that connection.
"""
from __future__ import annotations

import copy
from collections import deque
from datetime import datetime, timezone
from typing import Any

from .protocol import HASH_RE, VERSION, ProtocolError, ReporterConnection, validate_pairing, validate_report

__all__ = ["Reporter", "ReporterRecoveryError"]

_RECOVERY_HISTORY_LIMIT = 12


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _failure(error: Exception) -> dict[str, Any]:
    if isinstance(error, ProtocolError):
        reason = error.reason
        if not reason and error.status == 409 and str(error) == "Lease expired; reconnect and baseline":
            reason = "lease-expired"
        return {
            "outcome": "failed",
            "at": _now(),
            "status": error.status,
            "reason": reason or "protocol-error",
        }
    return {
        "outcome": "failed",
        "at": _now(),
        "status": None,
        "reason": type(error).__name__,
    }


class ReporterRecoveryError(ProtocolError):
    """A failed self-healing attempt with privacy-safe phase context."""

    def __init__(self, attempt: dict[str, Any], cause: Exception) -> None:
        phases = []
        for key, label in (
            ("initialReport", "initial-report"),
            ("reconnect", "reconnect"),
            ("baselineRetry", "baseline-retry"),
        ):
            phase = attempt[key]
            detail = phase["outcome"]
            if phase.get("status") is not None:
                detail += f"/{phase['status']}"
            if phase.get("reason"):
                detail += f"/{phase['reason']}"
            phases.append(f"{label}={detail}")
        cause_detail = str(cause) if isinstance(cause, ProtocolError) else type(cause).__name__
        message = f"Collector recovery {attempt['recoveryId']} failed: {'; '.join(phases)} ({cause_detail})"
        status = cause.status if isinstance(cause, ProtocolError) else 503
        super().__init__(message, status, reason="recovery-failed")
        self.recovery_id = attempt["recoveryId"]
        self.attempt = copy.deepcopy(attempt)
        self.dedup_key = ";".join(
            f"{key}:{attempt[key]['outcome']}:{attempt[key].get('status')}:{attempt[key].get('reason')}"
            for key in ("initialReport", "reconnect", "baselineRetry")
        )


class Reporter:
    def __init__(self, pairing: dict[str, Any], identity: dict[str, Any]) -> None:
        self.pairing = validate_pairing(pairing)
        self.identity = identity
        self.lease: str | None = None
        self.seq = 0
        self._conn = ReporterConnection(self.pairing)
        self._recovery_count = 0
        self._recovery_history: deque[dict[str, Any]] = deque(maxlen=_RECOVERY_HISTORY_LIMIT)

    def recovery_diagnostics(self) -> list[dict[str, Any]]:
        return copy.deepcopy(list(self._recovery_history))

    def _start_recovery(self, error: ProtocolError) -> dict[str, Any]:
        self._recovery_count += 1
        attempt = {
            "at": _now(),
            "recoveryId": f"r{self._recovery_count:04d}",
            "initialReport": _failure(error),
            "reconnect": {"outcome": "pending", "at": None, "status": None, "reason": None},
            "baselineRetry": {"outcome": "pending", "at": None, "status": None, "reason": None},
        }
        self._recovery_history.append(attempt)
        return attempt

    async def connect(self) -> None:
        response = await self._conn.request("/v1/connect", {
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
                response = await self._conn.request("/v1/report", report)
            except ProtocolError as error:
                if error.status == 409:
                    attempt = self._start_recovery(error)
                    try:
                        await self.connect()
                    except Exception as reconnect_error:
                        attempt["reconnect"] = _failure(reconnect_error)
                        attempt["baselineRetry"] = {
                            "outcome": "not-attempted", "at": _now(), "status": None, "reason": None,
                        }
                        raise ReporterRecoveryError(attempt, reconnect_error) from reconnect_error
                    attempt["reconnect"] = {
                        "outcome": "accepted", "at": _now(), "status": None, "reason": None,
                    }
                    report = validate_report({**report, "lease": self.lease, "seq": self.seq + 1})
                    try:
                        response = await self._conn.request("/v1/report", report)
                        if response.get("seq") != report["seq"]:
                            raise RuntimeError("Unexpected collector acknowledgement")
                    except Exception as retry_error:
                        attempt["baselineRetry"] = _failure(retry_error)
                        raise ReporterRecoveryError(attempt, retry_error) from retry_error
                    attempt["baselineRetry"] = {
                        "outcome": "accepted", "at": _now(), "status": None, "reason": None,
                    }
                elif error.status != 503:
                    raise
                else:
                    response = await self._conn.request("/v1/report", report)
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
        try:
            await self._conn.request("/v1/disconnect", {"lease": lease})
        finally:
            await self._conn.close()
