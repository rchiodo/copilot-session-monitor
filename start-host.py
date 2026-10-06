#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["copilot-session-monitor"]
#
# [tool.uv.sources]
# copilot-session-monitor = { path = ".", editable = true }
# ///
"""Start the collector (host) role.

Port of ``Start-Host-Headless.ps1`` (which just forwarded to
``scripts/Start-Role.ps1 -Role collector``). Run with::

    uv run start-host.py [--no-browser]

No prior ``pip install`` step is required: ``uv run`` installs this
project (editable, per the ``[tool.uv.sources]`` block above) into an
ephemeral virtual environment on first use.
"""
from __future__ import annotations

import argparse
import webbrowser
from pathlib import Path

from pymonitor.launcher import COLLECTOR_ROLE, LauncherError, start_role

ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Start the collector (host) role.")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open the dashboard in a browser once the collector is healthy.",
    )
    args = parser.parse_args()

    try:
        result = start_role(
            ROOT,
            COLLECTOR_ROLE,
            no_browser=args.no_browser,
            open_browser=webbrowser.open,
        )
    except LauncherError as error:
        print(f"error: {error}")
        return 1

    print(f"Collector is live at {result.runtime['url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
