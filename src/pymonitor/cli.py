"""Entry points: `pyproject.toml`'s `[project.scripts]` targets.

Ports the three standalone scripts that used to be run directly with `node`
(`server.mjs`, `watcher.mjs`, `configuration.mjs`) into installable console
entry points. There is deliberately no separate "tray" entry point:
`CollectorNativeTray`/`WatcherNativeTray` (see `tray_native.py`) run
in-process -- as of Phase 4 there is no `windows/tray.ps1` subprocess to
spawn at all. `host_main`/`client_main` are the only processes a launcher
script needs to start.
"""
from __future__ import annotations

import asyncio
import signal
import sys

from .configuration import configuration_command
from .server import CollectorServer
from .tray_native import CollectorNativeTray, WatcherNativeTray
from .watcher import Watcher

__all__ = ["host_main", "client_main", "config_main"]


async def _run_until_signalled(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            # add_signal_handler is Unix-only; Windows falls back to the
            # default KeyboardInterrupt/SIGINT handling, which still raises
            # into the surrounding asyncio.run() and is caught below.
            pass
    await stop.wait()


async def _host() -> None:
    server = CollectorServer()
    # `stop` is threaded into the tray so its "Stop collector" menu item can
    # unblock `_run_until_signalled` (and thus exit the process) in addition
    # to tearing down the server -- the native tray's "Stop" otherwise has
    # no OS-level signal to raise the way tray.ps1's subprocess exiting did.
    stop = asyncio.Event()
    bridge = CollectorNativeTray(server, stop)
    server.bridge = bridge
    await bridge.start()
    await server.start()
    try:
        await _run_until_signalled(stop)
    finally:
        await server.stop()


async def _client() -> None:
    watcher = Watcher()
    stop = asyncio.Event()
    bridge = WatcherNativeTray(watcher, stop)
    watcher.bridge = bridge
    await bridge.start()
    await watcher.start()
    try:
        await _run_until_signalled(stop)
    finally:
        await watcher.stop()


def host_main() -> None:
    """`pymonitor-host` -- ports `node src/server.mjs`."""
    try:
        asyncio.run(_host())
    except KeyboardInterrupt:
        pass


def client_main() -> None:
    """`pymonitor-client` -- ports `node src/watcher.mjs`."""
    try:
        asyncio.run(_client())
    except KeyboardInterrupt:
        pass


def config_main() -> None:
    """`pymonitor-config` -- ports `node src/configuration.mjs <command> ...`."""
    try:
        asyncio.run(configuration_command(sys.argv[1:]))
    except Exception as error:  # noqa: BLE001 - mirrors configuration.mjs's top-level catch.
        print(f"Configuration failed: {error}", file=sys.stderr)
        sys.exit(1)


_DISPATCH = {"host": host_main, "client": client_main, "config": config_main}


def _module_main() -> None:
    """Routes `python -m pymonitor.cli <host|client|config> [...]` so the
    PowerShell launchers can invoke a role without depending on the
    installed console-script shims being on PATH (pip warns they may not
    be, e.g. in a per-user install)."""
    if len(sys.argv) < 2 or sys.argv[1] not in _DISPATCH:
        print("usage: python -m pymonitor.cli {host|client|config} [args...]", file=sys.stderr)
        sys.exit(2)
    role = sys.argv.pop(1)
    _DISPATCH[role]()


if __name__ == "__main__":  # pragma: no cover - convenience for `python -m pymonitor.cli`
    _module_main()
