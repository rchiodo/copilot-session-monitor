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
network (see ``configuration.detect_lan_address``) and applies it directly,
equivalent to::

    uv run init-host.py <detected-ip> 43188 --reconfigure

Run with::

    uv run detect-lan-ip.py [--port 43188] [--dry-run]

``--dry-run`` only prints the detected address; it does not touch the
collector config. Stop the collector first (``uv run stop-host.py``) unless
using ``--dry-run`` -- same requirement as ``init-host.py``.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from pymonitor.configuration import configuration_command, detect_lan_address


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

    try:
        address = detect_lan_address()
    except RuntimeError as error:
        print(f"LAN IP detection failed: {error}", file=sys.stderr)
        return 1

    if args.dry_run:
        print(address)
        return 0

    print(f"Detected LAN IP: {address}")
    command = ["initialize", address, str(args.ingest_port), "replace"]
    try:
        asyncio.run(configuration_command(command))
    except Exception as error:  # noqa: BLE001 - mirrors init-host.py's top-level catch.
        print(f"Collector initialization failed: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
