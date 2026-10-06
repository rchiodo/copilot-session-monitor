#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["copilot-session-monitor"]
#
# [tool.uv.sources]
# copilot-session-monitor = { path = ".", editable = true }
# ///
"""Single entry point for the tray-based UX.

Port of ``Start-Tray.ps1``: with no arguments this starts the
watcher/child role (``Start-Client-Headless.ps1``); with ``--host`` it
starts the collector/host role instead (``Start-Host-Headless.ps1``).
Each mode is the unchanged existing role, with its own always-visible
tray icon offering role-appropriate clipboard-pairing menu items.

The original PowerShell script took a positional ``/host`` switch
(``Start-Tray.ps1 /host``); this port uses an argparse ``--host`` flag
instead, which is the idiomatic equivalent in a Python CLI. Run with::

    uv run start-tray.py [--host] [--no-browser]
"""
from __future__ import annotations

import argparse
import webbrowser
from pathlib import Path

from pymonitor.launcher import COLLECTOR_ROLE, WATCHER_ROLE, LauncherError, start_role

ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Start the tray-based host or watcher role.")
    parser.add_argument(
        "--host",
        action="store_true",
        help="Run the collector/host role instead of the watcher/child role.",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="(Host mode only) do not open the dashboard in a browser once healthy.",
    )
    args = parser.parse_args()

    try:
        if args.host:
            result = start_role(
                ROOT,
                COLLECTOR_ROLE,
                no_browser=args.no_browser,
                open_browser=webbrowser.open,
            )
            print(f"Collector is live at {result.runtime['url']}")
        else:
            result = start_role(ROOT, WATCHER_ROLE, no_browser=True)
            if result.paired_pending:
                print(
                    f"Watcher is running at {result.runtime['url']} but not yet paired. "
                    'Use the tray\'s "Connect to host..." menu to pair it.'
                )
            else:
                print(f"Watcher is live at {result.runtime['url']}")
    except LauncherError as error:
        print(f"error: {error}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
