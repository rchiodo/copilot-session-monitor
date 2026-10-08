"""Phase 4: native in-process Python tray (replaces the Phase 3
`tray_bridge.py` subprocess architecture).

Phase 3 reused `windows/tray.ps1` (a WinForms helper) exactly as the
original `server.mjs`/`watcher.mjs` did, spawning it as a child process and
talking over a line-delimited stdin/stdout JSON protocol
(`CollectorTrayBridge`/`WatcherTrayBridge` in `tray_bridge.py`). Phase 4
replaces that entire subprocess/IPC layer with a pure-Python implementation
running in-process: `pystray` draws the tray icon and context menu,
`win11toast` posts native Windows toast notifications, and a small
dedicated `win32gui` window receives `WM_POWERBROADCAST` (the one Windows
notification that genuinely requires a window procedure).

`windows/tray.ps1` is the authoritative behavior spec this module ports
(menu items, dialog semantics, balloon/toast throttling, 2s theme/process
poll cadence) -- it remains untouched in the repo (other `windows/*.ps1`
scripts still handle certificate/DACL setup, which is unrelated to this
change). See `docs/porting-notes.md` ("Phase 4") for the full behavioral
mapping and documented deviations (toast duration presets, no per-severity
toast icon, static non-theme-reactive tray icon bitmap, etc.).
"""
from __future__ import annotations

import asyncio
import queue
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
from typing import Any, Callable, TypeVar

import psutil
import pystray
from PIL import Image, ImageDraw

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

try:  # pragma: no cover - win11toast is Windows-only; guard for test/dev envs
    from win11toast import toast as _toast
except ImportError:  # pragma: no cover
    _toast = None  # type: ignore[assignment]

import tkinter as tk
from tkinter import messagebox, simpledialog

__all__ = ["CollectorNativeTray", "WatcherNativeTray"]

_PROCESS_NAMES = {"copilot.exe", "github.exe"}
_POLL_SECONDS = 2.0
_BALLOON_MIN_GAP_SECONDS = 10
# win11toast only offers 'short' (~7s) / 'long' (~25s) presets, not an exact
# millisecond duration; 'short' is the closest match to the original's fixed
# ShowBalloonTip(8000) (8s). See docs/porting-notes.md.
_TOAST_DURATION = "short"

# Standard Windows PBT_* power-broadcast codes (winuser.h); hardcoded here
# because PBT_APMRESUMEAUTOMATIC is not reliably exposed by every pywin32
# win32con build. Only Suspend/Resume are forwarded, matching tray.ps1's
# SystemEvents.PowerModeChanged filter (StatusChange events are ignored).
_PBT_APMSUSPEND = 0x4
_PBT_APMRESUMESUSPEND = 0x7
_PBT_APMRESUMEAUTOMATIC = 0x12


def _tray_image() -> Image.Image:
    """Generates a simple static icon in memory.

    The original uses `SystemIcons.Information` (a stock Windows icon) --
    there is no bundled .ico/.png asset in this repo to port, and the icon
    bitmap itself was never theme-reactive in the original (only the
    dashboard's CSS follows AppsUseLightTheme, via `on_theme`). This mirrors
    that: one static icon, identical for both host and watcher roles
    (differentiated only by tooltip/menu text, exactly like the original).
    """
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((2, 2, size - 2, size - 2), fill=(0, 102, 204, 255))
    draw.ellipse((size // 2 - 4, 11, size // 2 + 4, 19), fill=(255, 255, 255, 255))
    draw.rectangle((size // 2 - 4, 25, size // 2 + 4, size - 13), fill=(255, 255, 255, 255))
    return image


def _read_theme() -> str | None:
    """Port of `Get-Windows-AppTheme` (tray.ps1): HKCU AppsUseLightTheme."""
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
    """Port of tray.ps1's `Get-CimInstance Win32_Process -Filter
    "Name='copilot.exe' OR Name='github.exe'"` process snapshot. Note: the
    original's `-CollectorOnly` switch is declared but never actually
    passed by any launcher, so this always runs for both roles -- matching
    that (observed, not spec'd) behavior rather than "fixing" it.
    """
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
    """Background ~2s poll mirroring tray.ps1's shared timer-tick gate
    (`$script:nextProcesses`): Windows theme (collector only) + process
    snapshot (both roles). A single bad OS call (registry/psutil) in one
    tick is swallowed so the thread keeps polling, matching the original's
    graceful per-tick tolerance (LocalSource/FamilyMonitor already handle
    an absent/stale snapshot).
    """

    def __init__(
        self, push_theme: Callable[[str | None], None] | None, push_processes: Callable[[list[dict[str, Any]]], None]
    ) -> None:
        self._push_theme = push_theme
        self._push_processes = push_processes
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="pymonitor-tray-poll")

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
            print(f"Tray poll tick failed ({error})", file=sys.stderr)

    def _run(self) -> None:
        while not self._stop_event.wait(_POLL_SECONDS):
            self.tick()


class _PowerEventWindow:
    """Dedicated hidden window + message pump purely to receive
    `WM_POWERBROADCAST` (Suspend/Resume) -- the only Windows power
    notification that requires a window procedure. Kept separate from
    `_PollWorker` (a plain sleep loop) since nothing else needs a message
    pump; mirrors tray.ps1's `SystemEvents.PowerModeChanged` subscription.
    """

    def __init__(self, on_power: Callable[[str], None]) -> None:
        self._on_power = on_power
        self._hwnd: int | None = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="pymonitor-tray-power")

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


class _ToastWorker:
    """Background queue + >=10s-gap throttle before posting a toast,
    mirroring tray.ps1's `$script:nextBalloon` gate (prevents notification
    spam when several alerts fire close together). `show_system` bypasses
    the queue/throttle entirely, matching the original's immediate
    "connection string copied"/"Paired" balloons (shown outside the
    `notify`-driven alert queue).
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, monitor_url: str) -> None:
        self._loop = loop
        self._monitor_url = monitor_url or None
        self._queue: queue.Queue[tuple[dict[str, Any], Callable[[str], None]] | None] = queue.Queue()
        self._next_allowed = 0.0
        self._thread = threading.Thread(target=self._run, daemon=True, name="pymonitor-tray-toast")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._queue.put(None)

    def enqueue(self, notification: dict[str, Any], on_shown: Callable[[str], None]) -> bool:
        self._queue.put((notification, on_shown))
        return True

    def show_system(self, title: str, body: str) -> None:
        self._post(title, body)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            notification, on_shown = item
            now = time.monotonic()
            if now < self._next_allowed:
                time.sleep(self._next_allowed - now)
            self._post(notification["title"], notification["message"])
            self._next_allowed = time.monotonic() + _BALLOON_MIN_GAP_SECONDS
            self._loop.call_soon_threadsafe(on_shown, notification["id"])

    def _post(self, title: str, body: str) -> None:
        if _toast is None:
            print(f"[toast unavailable] {title}: {body}", file=sys.stderr)
            return
        try:
            _toast(title, body, on_click=self._monitor_url, duration=_TOAST_DURATION)
        except Exception as error:  # noqa: BLE001 - best effort, mirrors tray.ps1's fire-and-forget balloon tip
            print(f"Toast notification failed ({error})", file=sys.stderr)


_T = TypeVar("_T")


def _debug(message: str) -> None:
    """Diagnostic logging for the "Connect to host..." dialog-hang bug
    (dialog opens invisibly / whole tray menu freezes, with no exception
    ever raised -- see `_run_in_dedicated_thread` and `_run_connection_dialog`).

    Writes to stderr, which the launcher redirects to
    `.local/{role}-error.log`, with an explicit `flush=True`: the watcher
    process never exits under normal operation, and Python's default
    buffering for a stream redirected to a file would otherwise hold these
    lines unflushed indefinitely, making them useless for diagnosing a
    hang while the process is still alive.
    """
    print(f"[tray debug] {datetime.now(timezone.utc).isoformat()} {message}", file=sys.stderr, flush=True)


def _run_in_dedicated_thread(body: Callable[[], _T]) -> _T:
    """Run ``body`` on a brand-new, single-purpose thread and block the
    calling thread until it finishes, returning its result (or re-raising
    whatever it raised).

    Both `_run_label_dialog()` and `_run_connection_dialog()` are invoked
    from pystray's own menu-callback thread, which on Windows already drives
    its own native message pump for the tray icon. Creating a `tk.Tk()`
    window on that same thread is a documented source of dialogs that
    render but never respond to clicks (exactly the "OK does nothing"
    symptom this fixes) -- Tk's event loop and pystray's Win32 message pump
    both expect to own their thread. Running the Tk work on its own
    disposable thread, and blocking/joining from the caller, keeps those
    functions' existing synchronous call signature and return values
    unchanged; only the thread the Tk code actually executes on changes.
    """
    result: "queue.Queue[tuple[bool, Any]]" = queue.Queue(maxsize=1)

    def _runner() -> None:
        _debug("dedicated thread: entered, calling body()")
        try:
            value = body()
            _debug("dedicated thread: body() returned normally")
            result.put((True, value))
        except Exception as error:  # noqa: BLE001 - re-raised on the caller's thread below
            _debug(f"dedicated thread: body() raised {error!r}")
            result.put((False, error))

    _debug("caller: starting dedicated thread")
    threading.Thread(target=_runner, name="pymonitor-tray-dialog", daemon=True).start()
    _debug("caller: thread started, blocking on result queue")
    ok, value = result.get()
    _debug(f"caller: result queue returned ok={ok}")
    if not ok:
        raise value
    return value


def _force_foreground(window: Any) -> None:
    """Force a Tk window to render above everything else and take focus.

    Every dialog below runs on a disposable thread spun up from inside a
    pystray menu callback (see `_run_in_dedicated_thread`), so the process
    creating the window is essentially never the current Windows foreground
    application. Windows' focus-stealing prevention can then leave a
    brand-new window parked behind whatever else is on screen -- it never
    becomes visible, the user has no way to find or close it, and since
    `_run_in_dedicated_thread` blocks pystray's own callback thread on it,
    the *entire tray menu* then appears to hang (no new dialog, no further
    menu clicks work -- see the regression this fixes). `-topmost`, unlike
    `SetForegroundWindow`, doesn't require foreground permission and
    reliably makes the window appear on top regardless of which
    application currently has focus.
    """
    window.attributes("-topmost", True)
    window.lift()
    window.focus_force()


def _show_error(title: str, message: str) -> None:
    """Port of tray.ps1's `[System.Windows.Forms.MessageBox]::Show(...)`
    failure dialogs (connection request / connect-result failures)."""
    root = tk.Tk()
    root.withdraw()
    try:
        _force_foreground(root)
        messagebox.showerror(title, message, parent=root)
    finally:
        root.destroy()


def _run_label_dialog() -> str:
    """Port of tray.ps1's `InputBox` for the optional sub-machine label.

    VB's `InputBox` has no real "cancel" distinct from an empty string --
    Cancel returns "" just like leaving the box blank, and the original
    code unconditionally proceeds to generate a connection request either
    way (the blank/default label is filled in by `pair_connection`).
    `tkinter.simpledialog.askstring` returns `None` on Cancel, so that is
    coerced to "" here to preserve the original's "Cancel always proceeds"
    quirk rather than introducing a new ability to abort.

    The actual Tk dialog runs on a dedicated one-shot thread via
    `_run_in_dedicated_thread()` -- see that function's docstring for why
    (pystray's icon-callback thread already owns a native Win32 message
    pump, which conflicts with Tk's own event loop on the same thread).
    """

    def _body() -> str:
        root = tk.Tk()
        root.withdraw()
        try:
            _force_foreground(root)
            value = simpledialog.askstring(
                "Generate connection request",
                "Optional label for this sub machine (leave blank for a default name):",
                parent=root,
            )
        finally:
            root.destroy()
        return value or ""

    return _run_in_dedicated_thread(_body)


def _run_connection_dialog() -> str | None:
    """Port of tray.ps1's `Show-ConnectionDialog` (480x260 WinForms form
    with a multiline, scrollable paste box + OK/Cancel). Connection strings
    are inherently single-line base64 (see `protocol.py`), but a multiline
    `Text` widget is kept for visual/paste parity with the original, since
    strings can be up to 32KB long. Unlike the label dialog above, Cancel
    (or an empty/whitespace-only paste) here genuinely aborts -- returns
    `None` -- matching the original's `if ($null -ne $value)` guard.

    The actual Tk dialog runs on a dedicated one-shot thread via
    `_run_in_dedicated_thread()` -- see that function's docstring for why.
    """

    def _body() -> str | None:
        _debug("connection dialog: creating root Tk()")
        root = tk.Tk()
        _debug("connection dialog: root Tk() created, withdrawing")
        root.withdraw()
        result: dict[str, str | None] = {"value": None}
        try:
            _debug("connection dialog: creating Toplevel + widgets")
            dialog = tk.Toplevel(root)
            dialog.title("Connect to host")
            dialog.resizable(False, False)
            tk.Label(dialog, text="Paste the connection string copied from the host machine:").pack(
                padx=12, pady=(12, 4), anchor="w"
            )
            text = tk.Text(dialog, width=58, height=8, wrap="word")
            text.pack(padx=12, pady=4)
            buttons = tk.Frame(dialog)
            buttons.pack(padx=12, pady=(4, 12), anchor="e")

            def _ok() -> None:
                result["value"] = text.get("1.0", "end").strip() or None
                dialog.destroy()

            def _cancel() -> None:
                dialog.destroy()

            tk.Button(buttons, text="OK", width=8, command=_ok).pack(side="left", padx=4)
            tk.Button(buttons, text="Cancel", width=8, command=_cancel).pack(side="left")
            dialog.protocol("WM_DELETE_WINDOW", _cancel)
            dialog.transient(root)
            _debug("connection dialog: widgets built, forcing foreground")
            _force_foreground(dialog)
            _debug("connection dialog: calling grab_set()")
            dialog.grab_set()
            _debug("connection dialog: grab_set() returned, calling wait_window()")
            root.wait_window(dialog)
            _debug("connection dialog: wait_window() returned")
        finally:
            root.destroy()
            _debug("connection dialog: root destroyed")
        return result["value"]

    return _run_in_dedicated_thread(_body)


def _copy_to_clipboard(value: str) -> None:
    if win32clipboard is None:
        return
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardText(value, win32clipboard.CF_UNICODETEXT)
    finally:
        win32clipboard.CloseClipboard()


class CollectorNativeTray:
    """Native (non-subprocess) `TrayBridge` for `CollectorServer`.

    Satisfies the same `TrayBridge` Protocol `server.py` already defines
    (`process_snapshot`, `notify`, `generate_connection`, `on_stop`), so
    `CollectorServer` itself needed no changes for Phase 4 -- only the
    bridge implementation and `cli.py`'s construction changed.
    """

    def __init__(self, server: Any, stop_event: asyncio.Event) -> None:
        self._server = server
        self._stop_event = stop_event
        self._loop: asyncio.AbstractEventLoop | None = None
        self._icon: pystray.Icon | None = None
        self._poll: _PollWorker | None = None
        self._power: _PowerEventWindow | None = None
        self._toasts: _ToastWorker | None = None
        self._stopped = False

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        try:
            self._toasts = _ToastWorker(self._loop, self._server.url)
            self._toasts.start()
            self._poll = _PollWorker(self._push_theme, self._push_processes)
            self._poll.start()
            self._power = _PowerEventWindow(self._on_power)
            self._power.start()
            self._icon = pystray.Icon(
                "pymonitor-collector",
                _tray_image(),
                "Copilot session monitor - collector",
                menu=pystray.Menu(
                    pystray.MenuItem("Open observed sessions", self._on_open, default=True),
                    pystray.MenuItem("Test notification", self._on_test),
                    pystray.MenuItem("Generate connection request for a sub machine...", self._on_generate),
                    pystray.Menu.SEPARATOR,
                    pystray.MenuItem("Stop collector", self._on_stop_clicked),
                ),
            )
            threading.Thread(target=self._icon.run, daemon=True, name="pymonitor-collector-tray").start()
        except Exception as error:  # noqa: BLE001 - mirrors tray_bridge.py's spawn-failure path
            self._server.on_bridge_fault("Native tray unavailable")
            print(f"Native tray unavailable ({error})", file=sys.stderr)
            return
        self._server.on_bridge_ready()

    # -- menu actions (run on pystray's internal callback thread) ---------

    def _on_open(self, icon: Any, item: Any) -> None:
        webbrowser.open(self._server.url)

    def _on_test(self, icon: Any, item: Any) -> None:
        assert self._loop is not None
        asyncio.run_coroutine_threadsafe(self._server.test_notification(), self._loop)

    def _on_generate(self, icon: Any, item: Any) -> None:
        label = _run_label_dialog()
        assert self._loop is not None
        asyncio.run_coroutine_threadsafe(self._generate_connection(label), self._loop)

    def _on_stop_clicked(self, icon: Any, item: Any) -> None:
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(self._server.stop(), self._loop)
        future.add_done_callback(lambda _f: self._loop.call_soon_threadsafe(self._stop_event.set))

    async def _generate_connection(self, label: str) -> None:
        from .server import pair_connection

        try:
            value = await pair_connection(label)
        except Exception as error:  # noqa: BLE001 - mirrors tray.ps1's shared connect-result failure dialog
            print(f"Connection request failed ({type(error).__name__})", file=sys.stderr)
            _show_error("Connection failed", "Could not generate a connection string.")
            return
        _copy_to_clipboard(value)
        assert self._toasts is not None
        self._toasts.show_system(
            "Connection string copied",
            'Valid for pairing one machine. Paste it on the other PC using "Connect to host...".',
        )

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
        # `server.processes` via `_push_processes`; nothing reads this back
        # through the bridge on the collector side (unlike the watcher,
        # whose local observer pulls it via this exact method).
        return None

    async def notify(self, notification: dict[str, Any]) -> bool:
        if self._toasts is None:
            return False
        return self._toasts.enqueue(notification, self._mark_shown)

    def _mark_shown(self, notification_id: str) -> None:
        current = self._server.notification
        if current.get("id") == notification_id:
            current["state"] = "shown"

    async def generate_connection(self, label: str | None) -> None:
        await self._generate_connection(label or "")

    async def on_stop(self) -> None:
        # `CollectorServer.stop()` calls this as the last step of its own
        # shutdown (after all cleanup has finished), whether triggered by the
        # tray's "Stop collector" menu item or the HTTP `/api/stop` endpoint
        # (used by `stop-host.py`). Setting `_stop_event` here is what
        # unblocks `_run_until_signalled()` in `cli.py`'s `_host()` so the
        # process actually exits -- previously only the tray-click path did
        # this (via its own `add_done_callback`), so stopping via HTTP left
        # the process hung forever after `stop-host.py` reported success.
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
        if self._toasts:
            self._toasts.stop()
        if self._icon:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001 - best effort during shutdown
                pass


class WatcherNativeTray:
    """Native (non-subprocess) `TrayBridge` for `Watcher`.

    Satisfies `watcher.py`'s `TrayBridge` Protocol (`process_snapshot`,
    `on_health_changed`, `on_stop`); `on_health_changed` stays a no-op,
    mirroring tray.ps1's `-WatcherOnly` guard around the `notify` dispatch
    (watcher mode never shows alert toasts, only the connect-result/
    paired/power-adjacent system toasts handled directly below).
    """

    def __init__(self, watcher: Any, stop_event: asyncio.Event) -> None:
        self._watcher = watcher
        self._stop_event = stop_event
        self._loop: asyncio.AbstractEventLoop | None = None
        self._icon: pystray.Icon | None = None
        self._poll: _PollWorker | None = None
        self._power: _PowerEventWindow | None = None
        self._toasts: _ToastWorker | None = None
        self._processes: dict[str, Any] | None = None
        self._stopped = False

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._toasts = _ToastWorker(self._loop, "")
        self._toasts.start()
        # Theme push is collector-only (`-not $WatcherOnly` in tray.ps1);
        # the watcher still polls processes (the `-CollectorOnly` switch is
        # declared but never passed by any launcher -- see `_snapshot_processes`).
        self._poll = _PollWorker(None, self._push_processes)
        self._poll.start()
        self._power = _PowerEventWindow(self._on_power)
        self._power.start()
        self._icon = pystray.Icon(
            "pymonitor-watcher",
            _tray_image(),
            "Copilot session monitor - watcher",
            menu=pystray.Menu(
                pystray.MenuItem("Connect to host...", self._on_connect),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Stop watcher", self._on_stop_clicked),
            ),
        )
        threading.Thread(target=self._icon.run, daemon=True, name="pymonitor-watcher-tray").start()

    # -- menu actions (run on pystray's internal callback thread) ---------

    def _on_connect(self, icon: Any, item: Any) -> None:
        _debug("_on_connect: menu callback invoked, calling _run_connection_dialog()")
        value = _run_connection_dialog()
        _debug(f"_on_connect: _run_connection_dialog() returned (value is None: {value is None})")
        if value is None:
            return
        assert self._loop is not None
        asyncio.run_coroutine_threadsafe(self._apply_connection(value), self._loop)

    def _on_stop_clicked(self, icon: Any, item: Any) -> None:
        assert self._loop is not None
        future = asyncio.run_coroutine_threadsafe(self._watcher.stop(), self._loop)
        future.add_done_callback(lambda _f: self._loop.call_soon_threadsafe(self._stop_event.set))

    async def _apply_connection(self, value: str) -> None:
        try:
            result = await self._watcher.apply_connection_string(value)
        except Exception as error:  # noqa: BLE001 - mirrors tray.ps1's shared connect-result failure dialog
            _show_error("Connection failed", str(error))
            return
        assert self._toasts is not None
        self._toasts.show_system("Paired", f"Connected to {result['host']} as {result['label']}")

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
        # See `CollectorNativeTray.on_stop` -- same fix for the watcher/client
        # role's "Stop watcher" menu item vs. the `/stop` HTTP endpoint.
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
        if self._toasts:
            self._toasts.stop()
        if self._icon:
            try:
                self._icon.stop()
            except Exception:  # noqa: BLE001 - best effort during shutdown
                pass
