#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["copilot-session-monitor"]
#
# [tool.uv.sources]
# copilot-session-monitor = { path = ".", editable = true }
# ///
"""Stop a running watcher (client) instance.

Port of ``Stop-Client.ps1`` (which forwarded to
``scripts/Stop-Role.ps1 -Role watcher``). Run with::

    uv run stop-client.py
"""
from __future__ import annotations

from pathlib import Path

from pymonitor.launcher import WATCHER_ROLE, LauncherError, stop_role

ROOT = Path(__file__).resolve().parent


def main() -> int:
    try:
        stopped = stop_role(ROOT, WATCHER_ROLE)
    except LauncherError as error:
        print(f"error: {error}")
        return 1

    print("Watcher stopped." if stopped else "Watcher was not running.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
