#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["copilot-session-monitor"]
#
# [tool.uv.sources]
# copilot-session-monitor = { path = ".", editable = true }
# ///
"""Start the collector (host) role.

Runs the collector attached to this console: it prints directly to this
terminal (no `.local/collector.log` to tail) and stays in the foreground
until you type ``stop`` (or ``quit``/``exit``) at its prompt, or press
Ctrl+C. Run with::

    uv run start-host.py [--no-browser] [--lan [--lan-port PORT]]

No prior ``pip install`` step is required: ``uv run`` installs this
project (editable, per the ``[tool.uv.sources]`` block above) into an
ephemeral virtual environment on first use.

By default the collector binds loopback-only (127.0.0.1) -- no LAN
interface is opened. Pass ``--lan`` to make it reachable from other
machines on your network: this automates the manual
``stop-host.py`` -> ``detect-lan-ip.py`` dance into a single command.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from pymonitor.cli import host_main
from pymonitor.launcher import ensure_lan_bind

ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Start the collector (host) role.")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open the dashboard in a browser once the collector starts.",
    )
    parser.add_argument(
        "--lan",
        action="store_true",
        help=(
            "Before starting, stop any running collector, auto-detect this "
            "machine's LAN IP, and reconfigure the collector to bind to it "
            "instead of loopback-only. Automates stop-host.py + "
            "detect-lan-ip.py for multi-machine setup."
        ),
    )
    parser.add_argument(
        "--lan-port",
        type=int,
        default=43188,
        help="Ingest port to bind when --lan is used (default: 43188).",
    )
    args = parser.parse_args()

    if args.lan:
        try:
            address = ensure_lan_bind(ROOT, port=args.lan_port)
        except Exception as error:  # noqa: BLE001 - surfaced to the user, not re-raised.
            print(f"LAN reconfiguration failed: {error}")
            return 1
        print(f"Reconfigured collector to bind {address}:{args.lan_port}.")

    host_main(["--no-browser"] if args.no_browser else [])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
