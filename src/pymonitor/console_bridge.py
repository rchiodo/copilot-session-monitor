"""Console-based `TrayBridge` implementations (replaces `tray_native.py`).

`tray_native.py` drew a real Windows tray icon/menu (`pystray`), native
toast notifications (`win11toast`), and WinForms-style dialogs (`tkinter`)
for the "Generate connection request"/"Connect to host..." flows. Now that
`start-host.py`/`start-client.py` run attached to a visible console instead
of as a detached background process (see `launcher.py`), a plain
stdin/stdout command loop is a simpler, dependency-free substitute: no tray
icon, no toast permissions, no WinForms thread-affinity workarounds (see
`tray_native.py`'s `_run_in_dedicated_thread`/`_force_foreground`
docstrings for the bugs those existed to work around -- none of that
applies to a line-oriented console).

Ported unchanged from `tray_native.py` (no pystray/tkinter/win11toast
dependency): `_snapshot_processes()`, `_read_theme()`,
`_copy_to_clipboard()`, `_PollWorker`, `_PowerEventWindow`.

Notifications are printed directly instead of queued/throttled: the
`_ToastWorker`'s >=10s gap existed to avoid spamming the Windows toast
system, which does not apply to printing a line to an already-attached
console.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from typing import Any, Callable

import psutil

try:  # pragma: no cover - import guard for non-Windows dev/test environments
    import winreg
except ImportError:  # pragma: no cover
    winreg = None  # type: ignore[assignment]

try:  # pragma: no cover - import guard for non-Windows dev/test environments
    import win32clipboard
    import win32con
    import win32gui
except ImportError:  # pragma: no cover
    win32clipboard = win32con = win32gui = None  # type: ignore[assignment]

__all__ = ["CollectorConsoleBridge", "WatcherConsoleBridge"]

_PROCESS_NAMES = {"copilot.exe", "github.exe"}
_POLL_SECONDS = 2.0

# Standard Windows PBT_* power-broadcast codes (winuser.h); hardcoded here
# because PBT_APMRESUMEAUTOMATIC is not reliably exposed by every pywin32
# win32con build. Only Suspend/Resume are forwarded (see `tray_native.py`).
_PBT_APMSUSPEND = 0x4
_PBT_APMRESUMESUSPEND = 0x7
_PBT_APMRESUMEAUTOMATIC = 0x12


def _read_theme() -> str | None:
    """Port of `tray_native._read_theme` (HKCU AppsUseLightTheme)."""
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
        ) as key:
            value, _kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
        if value not in (0, 1):
            return None
        return "light" if value == 1 else "dark"
    except OSError:
        return None


def _snapshot_processes() -> list[dict[str, Any]]:
    """Port of `tray_native._snapshot_processes` (copilot.exe/github.exe)."""
    processes: list[dict[str, Any]] = []
    for proc in psutil.process_iter(["pid", "ppid", "name", "create_time"]):
        try:
            info = proc.info
            name = (info.get("name") or "").lower()
            if name not in _PROCESS_NAMES:
                continue
            started_at = datetime.fromtimestamp(info["create_time"], tz=timezone.utc).isoformat().replace(
                "+00:00", "Z"
            )
            processes.append(
                {"pid": info["pid"], "parentPid": info.get("ppid"), "name": name, "startedAt": started_at}
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    return processes


class _PollWorker:
    """Port of `tray_native._PollWorker` (unchanged)."""

    def __init__(
        self, push_theme: Callable[[str | None], None] | None, push_processes: Callable[[list[dict[str, Any]]], None]
    ) -> None:
        self._push_theme = push_theme
        self._push_processes = push_processes
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="pymonitor-console-poll")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def tick(self) -> None:
        """Runs one poll iteration; exposed directly for unit testing."""
        try:
            if self._push_theme is not None:
                self._push_theme(_read_theme())
            self._push_processes(_snapshot_processes())
        except Exception as error:  # noqa: BLE001 - keep polling even if a tick's OS calls fail
            print(f"Console poll tick failed ({error})", file=sys.stderr)

    def _run(self) -> None:
        while not self._stop_event.wait(_POLL_SECONDS):
            self.tick()


class _PowerEventWindow:
    """Port of `tray_native._PowerEventWindow` (unchanged): a dedicated
    hidden window + message pump purely to receive `WM_POWERBROADCAST`.
    """

    def __init__(self, on_power: Callable[[str], None]) -> None:
        self._on_power = on_power
        self._hwnd: int | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="pymonitor-console-power")

    def start(self) -> None:
        if win32gui is None:
            return
        self._thread.start()
        self._ready.wait(timeout=2)

    def stop(self) -> None:
        if win32gui is not None and self._hwnd:
            try:
                win32gui.PostMessage(self._hwnd, win32con.WM_CLOSE, 0, 0)
            except Exception:  # noqa: BLE001 - best effort during shutdown
                pass

    def _wndproc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == win32con.WM_POWERBROADCAST:
            if wparam == _PBT_APMSUSPEND:
                self._on_power("Suspend")
            elif wparam in (_PBT_APMRESUMESUSPEND, _PBT_APMRESUMEAUTOMATIC):
                self._on_power("Resume")
            return True
        if msg == win32con.WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _run(self) -> None:
        wc = win32gui.WNDCLASS()
        wc.lpfnWndProc = self._wndproc
        wc.lpszClassName = f"PymonitorPowerEvents{id(self)}"
        wc.hInstance = win32gui.GetModuleHandle(None)
        class_atom = win32gui.RegisterClass(wc)
        self._hwnd = win32gui.CreateWindow(
            class_atom, "pymonitor-power-events", 0, 0, 0, 0, 0, 0, 0, wc.hInstance, None
        )
        self._ready.set()
        win32gui.PumpMessages()


def _copy_to_clipboard(value: str) -> None:
    """Port of `tray_native._copy_to_clipboard` (unchanged)."""
    if win32clipboard is None:
        return
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(value, win32clipboard.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()


class CollectorConsoleBridge:
    """Console-based `TrayBridge` for `CollectorServer`.

    Satisfies the same `TrayBridge` Protocol `server.py` defines
    (`process_snapshot`, `notify`, `generate_connection`, `on_stop`).
    """

    def __init__(self, server: Any, stop_event: asyncio.Event) -> None:
        self._server = server
        self._stop_event = stop_event
        self._loop: asyncio.AbstractEventLoop | None = None
        self._poll: _PollWorker | None = None
        self._power: _PowerEventWindow | None = None
        self._console_thread: threading.Thread | None = None
        self._stopped = False

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._poll = _PollWorker(self._push_theme, self._push_processes)
        self._poll.start()
        self._power = _PowerEventWindow(self._on_power)
        self._power.start()
        print("Copilot session monitor - collector")
        print(f"Dashboard: {self._server.url}")
        self._print_help()
        self._console_thread = threading.Thread(
            target=self._console_loop, daemon=True, name="pymonitor-collector-console"
        )
        self._console_thread.start()
        self._server.on_bridge_ready()

    # -- console command loop (runs on a dedicated background thread) -----

    def _print_help(self) -> None:
        print(
            "Commands: help | open (dashboard) | test (notification) | "
            "generate (connection request) | recopy (connection string) | stop"
        )

    def _console_loop(self) -> None:
        while True:
            try:
                line = input().strip().lower()
            except (EOFError, OSError):
                return
            if not line:
                continue
            if line in ("help", "?"):
                self._print_help()
            elif line == "open":
                self._cmd_open()
            elif line == "test":
                self._cmd_test()
            elif line == "generate":
                self._cmd_generate()
            elif line == "recopy":
                self._cmd_recopy()
            elif line in ("stop", "quit", "exit"):
                self._cmd_stop()
                return
            else:
                print(f"Unknown command: {line!r}. Type 'help' for a list of commands.")

    def _cmd_open(self) -> None:
        webbrowser.open(self._server.url)
        print(f"Opened dashboard: {self._server.url}")

    def _cmd_test(self) -> None:
        assert self._loop is not None
        asyncio.run_coroutine_threadsafe(self._server.test_notification(), self._loop)

    def _cmd_generate(self) -> None:
        label = input("Optional label for this sub machine (blank for a default name): ").strip()
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(self._generate_connection(label), self._loop)
        future.result()

    def _cmd_recopy(self) -> None:
        from .server import remote_reporters_sync

        try:
            reporters = remote_reporters_sync()
        except Exception as error:  # noqa: BLE001 - mirrors tray_native's menu-building guard
            print(f"Could not list paired machines ({error})")
            return
        if not reporters:
            print("(no paired machines yet)")
            return
        print("Paired machines:")
        for index, reporter in enumerate(reporters, start=1):
            print(f"  {index}. {reporter['label']}")
        choice = input("Recopy connection string for # (blank to cancel): ").strip()
        if not choice:
            return
        try:
            reporter = reporters[int(choice) - 1]
        except (ValueError, IndexError):
            print("Invalid selection.")
            return
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(
            self._recopy_connection(reporter["id"], reporter["label"]), self._loop
        )
        future.result()

    def _cmd_stop(self) -> None:
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(self._server.stop(), self._loop)
        future.add_done_callback(lambda _f: self._loop.call_soon_threadsafe(self._stop_event.set))

    async def _generate_connection(self, label: str) -> None:
        from .server import pair_connection

        try:
            value = await pair_connection(label)
        except Exception as error:  # noqa: BLE001 - mirrors tray_native's shared failure path
            print(f"Connection request failed ({type(error).__name__})")
            return
        _copy_to_clipboard(value)
        print(
            'Connection string copied to clipboard. Valid for pairing one machine. '
            'Paste it on the other PC using the "connect" command.'
        )

    async def _recopy_connection(self, reporter_id: str, label: str) -> None:
        from .server import recopy_connection

        try:
            value = await recopy_connection(reporter_id)
        except Exception as error:  # noqa: BLE001 - mirrors _generate_connection's failure path
            print(f"Recopy connection string failed ({type(error).__name__})")
            return
        _copy_to_clipboard(value)
        print(f'Connection string for {label} copied to clipboard. Paste it on that machine using "connect".')

    # -- polling/power callbacks (run on background poll/power threads) ---

    def _push_theme(self, mode: str | None) -> None:
        assert self._loop is not None
        self._loop.call_soon_threadsafe(self._server.on_theme, mode)

    def _push_processes(self, processes: list[dict[str, Any]]) -> None:
        assert self._loop is not None
        snapshot = {"at": time.time() * 1000, "processes": processes}
        self._loop.call_soon_threadsafe(setattr, self._server, "processes", snapshot)

    def _on_power(self, mode: str) -> None:
        assert self._loop is not None
        asyncio.run_coroutine_threadsafe(self._server.on_power_event(), self._loop)

    # -- TrayBridge Protocol (server.py) -----------------------------------

    def process_snapshot(self) -> dict[str, Any] | None:
        # The collector pushes its own process snapshot straight onto
        # `server.processes` via `_push_processes`; see `tray_native.py`'s
        # matching method for why this always returns None.
        return None

    async def notify(self, notification: dict[str, Any]) -> bool:
        print(f"[notification] {notification['title']}: {notification['message']}")
        self._mark_shown(notification["id"])
        return True

    def _mark_shown(self, notification_id: str) -> None:
        current = self._server.notification
        if current.get("id") == notification_id:
            current["state"] = "shown"

    async def generate_connection(self, label: str | None) -> None:
        await self._generate_connection(label or "")

    async def on_stop(self) -> None:
        # See `tray_native.CollectorNativeTray.on_stop` -- unblocks
        # `_run_until_signalled()` in `cli.py`'s `_host()` whether shutdown
        # was triggered by the console's "stop" command or the HTTP
        # `/api/stop` endpoint (used by `stop-host.py`).
        self._shutdown()
        self._stop_event.set()

    def _shutdown(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._poll:
            self._poll.stop()
        if self._power:
            self._power.stop()


class WatcherConsoleBridge:
    """Console-based `TrayBridge` for `Watcher`.

    Satisfies `watcher.py`'s `TrayBridge` Protocol (`process_snapshot`,
    `on_health_changed`, `on_stop`).
    """

    def __init__(self, watcher: Any, stop_event: asyncio.Event) -> None:
        self._watcher = watcher
        self._stop_event = stop_event
        self._loop: asyncio.AbstractEventLoop | None = None
        self._poll: _PollWorker | None = None
        self._power: _PowerEventWindow | None = None
        self._console_thread: threading.Thread | None = None
        self._processes: dict[str, Any] | None = None
        self._stopped = False

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        # Theme push is collector-only, matching tray_native.py.
        self._poll = _PollWorker(None, self._push_processes)
        self._poll.start()
        self._power = _PowerEventWindow(self._on_power)
        self._power.start()
        print("Copilot session monitor - watcher")
        self._print_help()
        self._console_thread = threading.Thread(
            target=self._console_loop, daemon=True, name="pymonitor-watcher-console"
        )
        self._console_thread.start()

    # -- console command loop (runs on a dedicated background thread) -----

    def _print_help(self) -> None:
        print("Commands: help | connect (to host) | status | stop")

    def _console_loop(self) -> None:
        while True:
            try:
                line = input().strip().lower()
            except (EOFError, OSError):
                return
            if not line:
                continue
            if line in ("help", "?"):
                self._print_help()
            elif line == "connect":
                self._cmd_connect()
            elif line == "status":
                self._cmd_status()
            elif line in ("stop", "quit", "exit"):
                self._cmd_stop()
                return
            else:
                print(f"Unknown command: {line!r}. Type 'help' for a list of commands.")

    def _cmd_connect(self) -> None:
        value = input("Paste the connection string copied from the host machine: ").strip()
        if not value:
            print("Cancelled.")
            return
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(self._apply_connection(value), self._loop)
        future.result()

    def _cmd_status(self) -> None:
        if not self._watcher.pairing:
            print("Not paired yet. Use the 'connect' command to pair with a host.")
            return
        health = self._watcher.health
        print(
            f"Paired as '{self._watcher.pairing.get('label')}': "
            f"healthy={health.get('healthy')}, issue={health.get('issue')}"
        )

    def _cmd_stop(self) -> None:
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(self._watcher.stop(), self._loop)
        future.add_done_callback(lambda _f: self._loop.call_soon_threadsafe(self._stop_event.set))

    async def _apply_connection(self, value: str) -> None:
        try:
            result = await self._watcher.apply_connection_string(value)
        except Exception as error:  # noqa: BLE001 - mirrors tray_native's shared failure path
            print(f"Connection failed ({error})")
            return
        print(f"Paired. Connected to {result['host']} as {result['label']}")

    # -- polling/power callbacks (run on background poll/power threads) ---

    def _push_processes(self, processes: list[dict[str, Any]]) -> None:
        self._processes = {"at": time.time() * 1000, "processes": processes}

    def _on_power(self, mode: str) -> None:
        self._watcher.reset = "Windows process observation interrupted; rebaselining"
        self._processes = None

    # -- TrayBridge Protocol (watcher.py) -----------------------------------

    def process_snapshot(self) -> dict[str, Any] | None:
        return self._processes

    async def on_health_changed(self, health: dict[str, Any]) -> None:
        return None

    async def on_stop(self) -> None:
        # See `tray_native.WatcherNativeTray.on_stop` -- same fix for the
        # "stop" console command vs. the `/stop` HTTP endpoint.
        self._shutdown()
        self._stop_event.set()

    def _shutdown(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self._poll:
            self._poll.stop()
        if self._power:
            self._power.stop()
