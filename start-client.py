#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["copilot-session-monitor"]
#
# [tool.uv.sources]
# copilot-session-monitor = { path = ".", editable = true }
# ///
"""Start the watcher (client) role.

Runs the watcher attached to this console: it prints directly to this
terminal (no `.local/watcher.log` to tail) and stays in the foreground
until you type ``stop`` (or ``quit``/``exit``) at its prompt, or press
Ctrl+C. The watcher never has a dashboard page of its own, so unlike
``start-host.py`` there is no ``--no-browser`` flag here. Run with::

    uv run start-client.py
"""
from __future__ import annotations

from pymonitor.cli import client_main


def main() -> int:
    client_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
