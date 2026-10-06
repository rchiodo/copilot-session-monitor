#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["copilot-session-monitor"]
#
# [tool.uv.sources]
# copilot-session-monitor = { path = ".", editable = true }
# ///
"""Start the watcher (client) role.

Port of ``Start-Client-Headless.ps1`` (which forwarded to
``scripts/Start-Role.ps1 -Role watcher -NoBrowser``). The watcher never has
a dashboard page of its own, so unlike ``start-host.py`` there is no
``--no-browser`` flag to pass through -- a browser is never opened here,
matching the original script's hardcoded behavior. Run with::

    uv run start-client.py
"""
from __future__ import annotations

from pathlib import Path

from pymonitor.launcher import WATCHER_ROLE, LauncherError, start_role

ROOT = Path(__file__).resolve().parent


def main() -> int:
    try:
        result = start_role(ROOT, WATCHER_ROLE, no_browser=True)
    except LauncherError as error:
        print(f"error: {error}")
        return 1

    if result.paired_pending:
        print(
            f"Watcher is running at {result.runtime['url']} but not yet paired. "
            'Use the tray\'s "Connect to host..." menu to pair it.'
        )
    else:
        print(f"Watcher is live at {result.runtime['url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
