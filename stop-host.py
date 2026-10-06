#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["copilot-session-monitor"]
#
# [tool.uv.sources]
# copilot-session-monitor = { path = ".", editable = true }
# ///
"""Stop a running collector (host) instance.

Port of ``Stop-Host.ps1`` (which forwarded to
``scripts/Stop-Role.ps1 -Role collector``). Run with::

    uv run stop-host.py
"""
from __future__ import annotations

from pathlib import Path

from pymonitor.launcher import COLLECTOR_ROLE, LauncherError, stop_role

ROOT = Path(__file__).resolve().parent


def main() -> int:
    try:
        stopped = stop_role(ROOT, COLLECTOR_ROLE)
    except LauncherError as error:
        print(f"error: {error}")
        return 1

    print("Collector stopped." if stopped else "Collector was not running.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
