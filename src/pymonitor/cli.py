"""Entry points: `pyproject.toml`'s `[project.scripts]` targets.

Ports the three standalone scripts that used to be run directly with `node`
(`server.mjs`, `watcher.mjs`, `configuration.mjs`) into installable console
entry points. There is deliberately no separate "tray" entry point:
`CollectorConsoleBridge`/`WatcherConsoleBridge` (see `console_bridge.py`)
run in-process -- there is no detached subprocess to spawn at all.
`host_main`/`client_main` run attached to the launching console (blocking
until the "stop"/"quit" command or Ctrl+C) and are the only processes a
launcher script needs to start.
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import webbrowser

from .configuration import configuration_command
from .console_bridge import CollectorConsoleBridge, WatcherConsoleBridge
from .server import CollectorServer
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


async def _host(*, no_browser: bool = False) -> None:
    server = CollectorServer()
    # `stop` is threaded into the bridge so its "stop" console command can
    # unblock `_run_until_signalled` (and thus exit the process) in addition
    # to tearing down the server -- a console "stop" otherwise has no OS-
    # level signal to raise the way tray.ps1's subprocess exiting did.
    stop = asyncio.Event()
    bridge = CollectorConsoleBridge(server, stop)
    server.bridge = bridge
    await bridge.start()
    await server.start()
    # The launcher used to poll the (then-detached) collector's /api/status
    # over HTTP from the parent process until healthy, then open the
    # browser. Now that the collector runs attached in this same process,
    # it's already bridge_ready/healthy by the time server.start() returns,
    # so the browser can be opened immediately with no polling needed.
    if not no_browser:
        webbrowser.open(server.url)
    try:
        await _run_until_signalled(stop)
    finally:
        await server.stop()


async def _client() -> None:
    watcher = Watcher()
    stop = asyncio.Event()
    bridge = WatcherConsoleBridge(watcher, stop)
    watcher.bridge = bridge
    await bridge.start()
    await watcher.start()
    try:
        await _run_until_signalled(stop)
    finally:
        await watcher.stop()


def host_main(argv: list[str] | None = None) -> None:
    """`pymonitor-host` -- ports `node src/server.mjs`."""
    parser = argparse.ArgumentParser(prog="pymonitor-host")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open the dashboard in a browser once the collector starts.",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        asyncio.run(_host(no_browser=args.no_browser))
    except KeyboardInterrupt:
        pass
    except RuntimeError as error:  # e.g. acquire_role: a collector already owns .local/
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)


def client_main() -> None:
    """`pymonitor-client` -- ports `node src/watcher.mjs`."""
    try:
        asyncio.run(_client())
    except KeyboardInterrupt:
        pass
    except RuntimeError as error:  # e.g. acquire_role: a watcher already owns .local/
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)


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
