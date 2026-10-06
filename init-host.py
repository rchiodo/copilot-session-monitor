#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["copilot-session-monitor"]
#
# [tool.uv.sources]
# copilot-session-monitor = { path = ".", editable = true }
# ///
"""Initialize (or reconfigure) the collector's TLS/pairing identity.

Port of ``Initialize-Host.ps1``: a thin wrapper around
``python -m pymonitor.cli config initialize <BindAddress> <IngestPort>
[replace]``. Run with::

    uv run init-host.py [bind-address] [ingest-port] [--reconfigure]

Unlike the start/stop scripts this is a short-lived, synchronous
configuration operation rather than a background process to detach
from, so it calls ``configuration_command`` directly in-process instead
of spawning a subprocess.
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from pymonitor.configuration import configuration_command


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Initialize (or reconfigure) the collector's TLS/pairing identity."
    )
    parser.add_argument("bind_address", nargs="?", default="127.0.0.1")
    parser.add_argument("ingest_port", nargs="?", type=int, default=43188)
    parser.add_argument(
        "--reconfigure",
        action="store_true",
        help="Replace an existing identity instead of failing if one already exists.",
    )
    args = parser.parse_args()

    command = ["initialize", args.bind_address, str(args.ingest_port)]
    if args.reconfigure:
        command.append("replace")

    try:
        asyncio.run(configuration_command(command))
    except Exception as error:  # noqa: BLE001 - mirrors config_main's top-level catch.
        print(f"Collector initialization failed: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
