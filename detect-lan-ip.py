#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["copilot-session-monitor"]
#
# [tool.uv.sources]
# copilot-session-monitor = { path = ".", editable = true }
# ///
"""Detect this machine's LAN IP and (re)configure the collector to use it.

A fresh collector always starts loopback-only (``127.0.0.1``), which is
fine for self-observation but unreachable from another machine. Finding
the right address to pass to ``init-host.py`` is the main friction point in
multi-machine setup: ``ipconfig``/``Get-NetIPAddress`` often lists several
virtual adapters (Hyper-V switches, WSL) ahead of the real Wi-Fi/Ethernet
one. This script picks the address the OS itself would use to reach the
network (see ``configuration.detect_lan_address``) and applies it via
``launcher.ensure_lan_bind`` -- the same no-op-safe path
``start-host.py --lan`` uses: if the collector is already configured for
this exact address and port, nothing is touched. In particular, the TLS
certificate is *not* regenerated when nothing changed, so already-paired
remote watchers keep working. Only when the detected address or port
actually differs does this stop the collector, reconfigure, and rotate the
certificate -- equivalent to the old manual ``stop-host.py`` ->
``init-host.py <detected-ip> <port> --reconfigure`` -> ``start-tray.py
--host`` dance (which this script used to perform unconditionally on every
run, even when nothing had changed -- silently invalidating every paired
remote watcher's pinned certificate each time).

Run with::

    uv run detect-lan-ip.py [ingest-port] [--dry-run]

``--dry-run`` only prints the detected address; it does not touch the
collector config or stop any running collector.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pymonitor.configuration import detect_lan_address
from pymonitor.launcher import ensure_lan_bind

ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Detect this machine's LAN IP and (re)configure the collector to use it."
    )
    parser.add_argument("ingest_port", nargs="?", type=int, default=43188)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print the detected address; do not change the collector config.",
    )
    args = parser.parse_args()

    if args.dry_run:
        try:
            address = detect_lan_address()
        except RuntimeError as error:
            print(f"LAN IP detection failed: {error}", file=sys.stderr)
            return 1
        print(address)
        return 0

    try:
        address = ensure_lan_bind(ROOT, port=args.ingest_port)
    except Exception as error:  # noqa: BLE001 - mirrors start-host.py's --lan handling.
        print(f"Collector initialization failed: {error}", file=sys.stderr)
        return 1

    print(f"Collector bound to {address}:{args.ingest_port}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
