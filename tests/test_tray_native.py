"""Tests for pymonitor.tray_native (Phase 4: replaces the Phase 3
subprocess-based `tray_bridge.py`/`test_tray_bridge.py`).

Since the native tray talks directly to Windows-only APIs (`pystray`,
`win11toast`, `winreg`, `win32gui`/`win32con`/`win32clipboard`, `psutil`
process enumeration) instead of spawning a child process, these tests
monkeypatch those module-level references on `pymonitor.tray_native`
rather than faking a child process's stdio, as `test_tray_bridge.py` did.
`pystray`/`PIL` import correctly on non-Windows platforms (pure Python),
so only the Windows-specific modules (`winreg`, `win32*`, `win11toast`)
and `psutil` process enumeration need faking here.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pymonitor import tray_native as tn
from pymonitor.tray_native import CollectorNativeTray, WatcherNativeTray


def _fake_server() -> MagicMock:
    server = MagicMock()
    server.url = "https://127.0.0.1:43187/"
    server.bridge_ready = False
    server.processes = None
    server.notification = {}
    server.test_notification = AsyncMock()
    server.stop = AsyncMock()
    server.on_power_event = AsyncMock()
    server.on_theme = MagicMock()
    server.on_bridge_ready = MagicMock()
    server.on_bridge_fault = MagicMock()
    return server


def _fake_watcher() -> MagicMock:
    watcher = MagicMock()
    watcher.reset = None
    watcher.stop = AsyncMock()
    watcher.apply_connection_string = AsyncMock()
    return watcher


@pytest.fixture(autouse=True)
def _fake_icon(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replaces pystray.Icon so no real tray/thread backend is required."""
    created: list[MagicMock] = []

    def _factory(*args: Any, **kwargs: Any) -> MagicMock:
        icon = MagicMock()
        icon.run = MagicMock()
        created.append(icon)
        return icon

    monkeypatch.setattr(tn.pystray, "Icon", _factory)
    monkeypatch.setattr(tn.threading, "Thread", lambda target, daemon, name: MagicMock(start=target))
    return created


@pytest.fixture(autouse=True)
def _no_poll_power(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevents the background poll/power threads from actually starting
    during bridge .start() calls that aren't specifically testing them --
    individual poll/power tests construct these classes directly instead.
    """
    monkeypatch.setattr(tn._PollWorker, "start", lambda self: None)
    monkeypatch.setattr(tn._PowerEventWindow, "start", lambda self: None)
    monkeypatch.setattr(tn._ToastWorker, "start", lambda self: None)


# -- CollectorNativeTray --------------------------------------------------


async def test_collector_tray_start_marks_bridge_ready() -> None:
    server = _fake_server()
    tray = CollectorNativeTray(server, asyncio.Event())
    await tray.start()
    server.on_bridge_ready.assert_called_once()


async def test_collector_tray_start_fault_on_icon_error(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _fake_server()

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no display")

    monkeypatch.setattr(tn.pystray, "Icon", _boom)
    tray = CollectorNativeTray(server, asyncio.Event())
    await tray.start()
    server.on_bridge_fault.assert_called_once()
    server.on_bridge_ready.assert_not_called()


async def test_collector_tray_open_opens_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _fake_server()
    tray = CollectorNativeTray(server, asyncio.Event())
    await tray.start()
    opened: list[str] = []
    monkeypatch.setattr(tn.webbrowser, "open", opened.append)
    tray._on_open(None, None)
    assert opened == [server.url]


async def test_collector_tray_test_notification_schedules_coroutine() -> None:
    server = _fake_server()
    tray = CollectorNativeTray(server, asyncio.Event())
    await tray.start()
    tray._on_test(None, None)
    await asyncio.sleep(0)
    server.test_notification.assert_called_once()


async def test_collector_tray_stop_sets_stop_event_after_server_stops() -> None:
    server = _fake_server()
    stop_event = asyncio.Event()
    tray = CollectorNativeTray(server, stop_event)
    await tray.start()
    tray._on_stop_clicked(None, None)
    await asyncio.wait_for(stop_event.wait(), timeout=1)
    server.stop.assert_called_once()
    assert stop_event.is_set()


async def test_collector_tray_notify_enqueues_and_updates_notification() -> None:
    server = _fake_server()
    server.notification = {"id": "abc", "state": "queued", "title": "t", "message": "m"}
    tray = CollectorNativeTray(server, asyncio.Event())
    await tray.start()
    accepted = await tray.notify(server.notification)
    assert accepted is True
    tray._mark_shown("abc")
    assert server.notification["state"] == "shown"


async def test_collector_tray_mark_shown_ignores_stale_id() -> None:
    server = _fake_server()
    server.notification = {"id": "current", "state": "queued"}
    tray = CollectorNativeTray(server, asyncio.Event())
    await tray.start()
    tray._mark_shown("stale")
    assert server.notification["state"] == "queued"


async def test_collector_tray_process_snapshot_returns_none() -> None:
    server = _fake_server()
    tray = CollectorNativeTray(server, asyncio.Event())
    await tray.start()
    assert tray.process_snapshot() is None


async def test_collector_tray_push_theme_and_processes() -> None:
    server = _fake_server()
    tray = CollectorNativeTray(server, asyncio.Event())
    await tray.start()
    tray._push_theme("dark")
    await asyncio.sleep(0)
    server.on_theme.assert_called_once_with("dark")

    tray._push_processes([{"pid": 1, "parentPid": None, "name": "copilot.exe", "startedAt": "x"}])
    await asyncio.sleep(0)
    assert server.processes is not None
    assert server.processes["processes"][0]["name"] == "copilot.exe"


async def test_collector_tray_on_power_triggers_server_power_event() -> None:
    server = _fake_server()
    tray = CollectorNativeTray(server, asyncio.Event())
    await tray.start()
    tray._on_power("Suspend")
    await asyncio.sleep(0)
    server.on_power_event.assert_called_once()


async def test_collector_tray_generate_connection_success_copies_clipboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _fake_server()
    tray = CollectorNativeTray(server, asyncio.Event())
    await tray.start()

    async def _fake_pair_connection(label: str | None) -> str:
        assert label == "my-label"
        return "csm1:xyz"

    monkeypatch.setattr("pymonitor.server.pair_connection", _fake_pair_connection)
    copied: list[str] = []
    monkeypatch.setattr(tn, "_copy_to_clipboard", copied.append)
    shown: list[tuple[str, str]] = []
    tray._toasts.show_system = lambda title, body: shown.append((title, body))

    await tray._generate_connection("my-label")
    assert copied == ["csm1:xyz"]
    assert shown and shown[0][0] == "Connection string copied"


async def test_collector_tray_generate_connection_failure_shows_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = _fake_server()
    tray = CollectorNativeTray(server, asyncio.Event())
    await tray.start()

    async def _fake_pair_connection(label: str | None) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr("pymonitor.server.pair_connection", _fake_pair_connection)
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(tn, "_show_error", lambda title, message: errors.append((title, message)))

    await tray._generate_connection("")
    assert errors and errors[0][0] == "Connection failed"


async def test_collector_tray_shutdown_is_idempotent() -> None:
    server = _fake_server()
    tray = CollectorNativeTray(server, asyncio.Event())
    await tray.start()
    tray._poll.stop = MagicMock()
    tray._power.stop = MagicMock()
    tray._toasts.stop = MagicMock()
    tray._shutdown()
    tray._shutdown()
    tray._poll.stop.assert_called_once()
    tray._power.stop.assert_called_once()
    tray._toasts.stop.assert_called_once()
    tray._icon.stop.assert_called_once()


# -- WatcherNativeTray ------------------------------------------------------


async def test_watcher_tray_start_builds_icon_without_default_item() -> None:
    watcher = _fake_watcher()
    tray = WatcherNativeTray(watcher, asyncio.Event())
    await tray.start()
    assert tray._icon is not None


async def test_watcher_tray_connect_dialog_cancel_skips_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    """True-cancel semantics: unlike the host's label dialog, a Cancel (or
    blank paste) here must NOT call apply_connection_string at all."""
    watcher = _fake_watcher()
    tray = WatcherNativeTray(watcher, asyncio.Event())
    await tray.start()
    monkeypatch.setattr(tn, "_run_connection_dialog", lambda: None)
    tray._on_connect(None, None)
    await asyncio.sleep(0)
    watcher.apply_connection_string.assert_not_called()


async def test_watcher_tray_connect_dialog_value_applies_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    watcher = _fake_watcher()
    watcher.apply_connection_string.return_value = {"host": "HOSTPC", "label": "sub1"}
    tray = WatcherNativeTray(watcher, asyncio.Event())
    await tray.start()
    monkeypatch.setattr(tn, "_run_connection_dialog", lambda: "csm1:abc")
    shown: list[tuple[str, str]] = []
    tray._toasts.show_system = lambda title, body: shown.append((title, body))
    tray._on_connect(None, None)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    watcher.apply_connection_string.assert_called_once_with("csm1:abc")
    assert shown and shown[0] == ("Paired", "Connected to HOSTPC as sub1")


async def test_watcher_tray_apply_connection_failure_shows_error(monkeypatch: pytest.MonkeyPatch) -> None:
    watcher = _fake_watcher()
    watcher.apply_connection_string.side_effect = RuntimeError("bad string")
    tray = WatcherNativeTray(watcher, asyncio.Event())
    await tray.start()
    errors: list[tuple[str, str]] = []
    monkeypatch.setattr(tn, "_show_error", lambda title, message: errors.append((title, message)))
    await tray._apply_connection("csm1:bad")
    assert errors and errors[0][0] == "Connection failed"


async def test_watcher_tray_stop_sets_stop_event_after_watcher_stops() -> None:
    watcher = _fake_watcher()
    stop_event = asyncio.Event()
    tray = WatcherNativeTray(watcher, stop_event)
    await tray.start()
    tray._on_stop_clicked(None, None)
    await asyncio.wait_for(stop_event.wait(), timeout=1)
    watcher.stop.assert_called_once()
    assert stop_event.is_set()


async def test_watcher_tray_health_changed_is_noop() -> None:
    watcher = _fake_watcher()
    tray = WatcherNativeTray(watcher, asyncio.Event())
    await tray.start()
    assert await tray.on_health_changed({"ok": False}) is None


async def test_watcher_tray_process_snapshot_reflects_last_poll() -> None:
    watcher = _fake_watcher()
    tray = WatcherNativeTray(watcher, asyncio.Event())
    await tray.start()
    assert tray.process_snapshot() is None
    tray._push_processes([{"pid": 5, "parentPid": None, "name": "github.exe", "startedAt": "x"}])
    snapshot = tray.process_snapshot()
    assert snapshot is not None
    assert snapshot["processes"][0]["name"] == "github.exe"


async def test_watcher_tray_on_power_resets_watcher_and_clears_processes() -> None:
    watcher = _fake_watcher()
    tray = WatcherNativeTray(watcher, asyncio.Event())
    await tray.start()
    tray._push_processes([{"pid": 1, "parentPid": None, "name": "copilot.exe", "startedAt": "x"}])
    tray._on_power("Resume")
    assert watcher.reset is not None
    assert tray.process_snapshot() is None


# -- _PollWorker / _read_theme / _snapshot_processes (graceful degradation) --


def test_poll_worker_tick_calls_push_theme_and_processes() -> None:
    themes: list[str | None] = []
    processes: list[list[dict[str, Any]]] = []
    worker = tn._PollWorker(themes.append, processes.append)
    worker.tick()
    assert len(themes) == 1
    assert len(processes) == 1


def test_poll_worker_tick_swallows_theme_exception(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    def _boom(mode: Any) -> None:
        raise RuntimeError("theme push failed")

    processes: list[Any] = []
    worker = tn._PollWorker(_boom, processes.append)
    worker.tick()  # must not raise
    assert processes == []  # push_processes never reached since push_theme raised first


def test_read_theme_returns_none_when_winreg_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tn, "winreg", None)
    assert tn._read_theme() is None


def test_read_theme_maps_registry_value(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_winreg = MagicMock()
    fake_winreg.HKEY_CURRENT_USER = 1
    fake_winreg.QueryValueEx.return_value = (1, 4)
    fake_winreg.OpenKey.return_value.__enter__.return_value = MagicMock()
    monkeypatch.setattr(tn, "winreg", fake_winreg)
    assert tn._read_theme() == "light"

    fake_winreg.QueryValueEx.return_value = (0, 4)
    assert tn._read_theme() == "dark"


def test_read_theme_returns_none_on_os_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_winreg = MagicMock()
    fake_winreg.OpenKey.side_effect = OSError("missing key")
    monkeypatch.setattr(tn, "winreg", fake_winreg)
    assert tn._read_theme() is None


def test_snapshot_processes_filters_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeProc:
        def __init__(self, info: dict[str, Any]) -> None:
            self.info = info

    fake_procs = [
        _FakeProc({"pid": 1, "ppid": 0, "name": "copilot.exe", "create_time": time.time()}),
        _FakeProc({"pid": 2, "ppid": 0, "name": "notepad.exe", "create_time": time.time()}),
        _FakeProc({"pid": 3, "ppid": 1, "name": "github.exe", "create_time": time.time()}),
    ]
    monkeypatch.setattr(tn.psutil, "process_iter", lambda attrs: fake_procs)
    snapshot = tn._snapshot_processes()
    names = {entry["name"] for entry in snapshot}
    assert names == {"copilot.exe", "github.exe"}


def test_snapshot_processes_skips_vanished_process(monkeypatch: pytest.MonkeyPatch) -> None:
    class _VanishingProc:
        @property
        def info(self) -> dict[str, Any]:
            raise tn.psutil.NoSuchProcess(pid=1)

    monkeypatch.setattr(tn.psutil, "process_iter", lambda attrs: [_VanishingProc()])
    assert tn._snapshot_processes() == []


# -- dialog Cancel-semantics asymmetry (critical fidelity requirement) ------


def test_label_dialog_coerces_cancel_to_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """VB's InputBox has no real Cancel-abort: this must return "" (never
    raise/abort) so the caller always proceeds to generate a connection."""
    monkeypatch.setattr(tn.simpledialog, "askstring", lambda *a, **k: None)
    monkeypatch.setattr(tn.tk, "Tk", lambda: MagicMock())
    assert tn._run_label_dialog() == ""


def test_label_dialog_passes_through_typed_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tn.simpledialog, "askstring", lambda *a, **k: "laptop-2")
    monkeypatch.setattr(tn.tk, "Tk", lambda: MagicMock())
    assert tn._run_label_dialog() == "laptop-2"
