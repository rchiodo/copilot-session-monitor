#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["copilot-session-monitor"]
#
# [tool.uv.sources]
# copilot-session-monitor = { path = ".", editable = true }
# ///
"""Single entry point for both roles.

With no arguments this starts the watcher/client role; with ``--host`` it
starts the collector/host role instead. Each mode runs attached to this
console -- it prints directly to this terminal and stays in the
foreground until you type ``stop``/``quit``/``exit`` at its prompt, or
press Ctrl+C.

Run with::

    uv run start-tray.py [--host] [--no-browser] [--lan [--lan-port PORT]]

``--lan`` (host mode only) makes the collector reachable from other
machines on your network instead of loopback-only (127.0.0.1): it
automates the manual ``stop-host.py`` -> ``detect-lan-ip.py`` dance
into this single command.
"""
from __future__ import annotations

import argparse
from pathlib import Path

from pymonitor.cli import client_main, host_main
from pymonitor.launcher import ensure_lan_bind

ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Start the collector (host) or watcher (client) role.")
    parser.add_argument(
        "--host",
        action="store_true",
        help="Run the collector/host role instead of the watcher/client role.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="(Host mode only) do not open the dashboard in a browser once started.",
    )
    parser.add_argument(
        "--lan",
        action="store_true",
        help=(
            "(Host mode only) before starting, stop any running collector, "
            "auto-detect this machine's LAN IP, and reconfigure the "
            "collector to bind to it instead of loopback-only."
        ),
    )
    parser.add_argument(
        "--lan-port",
        type=int,
        default=43188,
        help="Ingest port to bind when --lan is used (default: 43188).",
    )
    args = parser.parse_args()

    if args.lan and not args.host:
        print("error: --lan only applies to the collector/host role (pass --host too).")
        return 1

    if args.lan:
        try:
            address = ensure_lan_bind(ROOT, port=args.lan_port)
        except Exception as error:  # noqa: BLE001 - surfaced to the user, not re-raised.
            print(f"LAN reconfiguration failed: {error}")
            return 1
        print(f"Reconfigured collector to bind {address}:{args.lan_port}.")

    if args.host:
        host_main(["--no-browser"] if args.no_browser else [])
    else:
        client_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
