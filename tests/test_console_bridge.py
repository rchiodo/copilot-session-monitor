"""Tests for pymonitor.console_bridge (replaces the deleted
`tray_native.py`/`test_tray_native.py`).

Unlike the native tray, the console bridge has no pystray icon/menu and no
tkinter dialogs -- user interaction is a plain `input()` command loop on a
background thread. These tests avoid ever starting that real thread (it
would block on real stdin); instead they monkeypatch `threading.Thread` so
`.start()` is a no-op, and exercise the `_cmd_*`/`_console_loop` methods
directly by feeding fake `input()` answers.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from pymonitor import console_bridge as cb
from pymonitor.console_bridge import CollectorConsoleBridge, WatcherConsoleBridge


def _fake_server() -> MagicMock:
    server = MagicMock()
    server.url = "https://127.0.0.1:43187/"
    server.processes = None
    server.notification = {}
    server.test_notification = AsyncMock()
    server.stop = AsyncMock()
    server.on_power_event = AsyncMock()
    server.on_theme = MagicMock()
    server.on_bridge_ready = MagicMock()
    return server


def _fake_watcher() -> MagicMock:
    watcher = MagicMock()
    watcher.reset = None
    watcher.pairing = None
    watcher.health = {}
    watcher.stop = AsyncMock()
    watcher.apply_connection_string = AsyncMock()
    return watcher


_REAL_THREAD = cb.threading.Thread


def _fake_thread_ctor(*args: Any, target: Any = None, daemon: Any = None, name: str | None = None, **kwargs: Any) -> Any:
    """Intercepts only the bridges' own named threads (console loop,
    poll worker, power window) so they never actually start -- the console
    thread blocks on real `input()` and would hang the test process.
    Anything else (e.g. ThreadPoolExecutor's worker threads used by
    `loop.run_in_executor` in these tests) is passed through to the real
    `threading.Thread`, since `cb.threading` *is* the actual `threading`
    module (patching it here affects the whole process, not a copy)."""
    if name and name.startswith("pymonitor-"):
        return MagicMock()
    return _REAL_THREAD(*args, target=target, daemon=daemon, name=name, **kwargs)


@pytest.fixture(autouse=True)
def _no_background_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Prevents the real poll/power/console threads from starting during
    bridge .start() calls -- the underlying methods are exercised directly
    instead of through the actual threaded loops (which would block on
    real stdin or OS APIs)."""
    monkeypatch.setattr(cb._PollWorker, "start", lambda self: None)
    monkeypatch.setattr(cb._PollWorker, "stop", lambda self: None)
    monkeypatch.setattr(cb._PowerEventWindow, "start", lambda self: None)
    monkeypatch.setattr(cb._PowerEventWindow, "stop", lambda self: None)
    monkeypatch.setattr(cb.threading, "Thread", _fake_thread_ctor)


# -- CollectorConsoleBridge --------------------------------------------------


async def test_collector_bridge_start_marks_bridge_ready_and_prints_url(capsys: Any) -> None:
    server = _fake_server()
    bridge = CollectorConsoleBridge(server, asyncio.Event())
    await bridge.start()
    server.on_bridge_ready.assert_called_once()
    assert server.url in capsys.readouterr().out


async def test_collector_bridge_process_snapshot_is_always_none() -> None:
    server = _fake_server()
    bridge = CollectorConsoleBridge(server, asyncio.Event())
    await bridge.start()
    assert bridge.process_snapshot() is None


async def test_collector_bridge_notify_prints_and_marks_shown(capsys: Any) -> None:
    server = _fake_server()
    bridge = CollectorConsoleBridge(server, asyncio.Event())
    await bridge.start()
    server.notification = {"id": "abc"}
    result = await bridge.notify({"id": "abc", "title": "Hi", "message": "there"})
    assert result is True
    assert "Hi: there" in capsys.readouterr().out
    assert server.notification["state"] == "shown"


async def test_collector_bridge_notify_ignores_stale_id() -> None:
    server = _fake_server()
    bridge = CollectorConsoleBridge(server, asyncio.Event())
    await bridge.start()
    server.notification = {"id": "current"}
    await bridge.notify({"id": "stale", "title": "x", "message": "y"})
    assert "state" not in server.notification


async def test_collector_bridge_cmd_open_opens_browser(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    server = _fake_server()
    bridge = CollectorConsoleBridge(server, asyncio.Event())
    await bridge.start()
    opened: list[str] = []
    monkeypatch.setattr(cb.webbrowser, "open", opened.append)
    bridge._cmd_open()
    assert opened == [server.url]
    assert server.url in capsys.readouterr().out


async def test_collector_bridge_cmd_test_schedules_test_notification() -> None:
    server = _fake_server()
    bridge = CollectorConsoleBridge(server, asyncio.Event())
    await bridge.start()
    bridge._cmd_test()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    server.test_notification.assert_called_once()


async def test_collector_bridge_generate_connection_success_copies_clipboard(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    server = _fake_server()
    bridge = CollectorConsoleBridge(server, asyncio.Event())
    await bridge.start()

    async def _fake_pair_connection(label: str | None) -> str:
        assert label == "my-label"
        return "csm1:xyz"

    monkeypatch.setattr("pymonitor.server.pair_connection", _fake_pair_connection)
    copied: list[str] = []
    monkeypatch.setattr(cb, "_copy_to_clipboard", copied.append)

    await bridge._generate_connection("my-label")
    assert copied == ["csm1:xyz"]
    assert "copied to clipboard" in capsys.readouterr().out


async def test_collector_bridge_generate_connection_failure_prints_error(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    server = _fake_server()
    bridge = CollectorConsoleBridge(server, asyncio.Event())
    await bridge.start()

    async def _fake_pair_connection(label: str | None) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr("pymonitor.server.pair_connection", _fake_pair_connection)
    await bridge._generate_connection("")
    assert "Connection request failed" in capsys.readouterr().out


async def test_collector_bridge_recopy_connection_success_copies_clipboard(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    server = _fake_server()
    bridge = CollectorConsoleBridge(server, asyncio.Event())
    await bridge.start()

    async def _fake_recopy_connection(reporter_id: str) -> str:
        assert reporter_id == "aaa"
        return "csm1:xyz"

    monkeypatch.setattr("pymonitor.server.recopy_connection", _fake_recopy_connection)
    copied: list[str] = []
    monkeypatch.setattr(cb, "_copy_to_clipboard", copied.append)

    await bridge._recopy_connection("aaa", "rchiodo-bigboy")
    assert copied == ["csm1:xyz"]
    out = capsys.readouterr().out
    assert "rchiodo-bigboy" in out and "copied to clipboard" in out


async def test_collector_bridge_recopy_connection_failure_prints_error(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    server = _fake_server()
    bridge = CollectorConsoleBridge(server, asyncio.Event())
    await bridge.start()

    async def _fake_recopy_connection(reporter_id: str) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr("pymonitor.server.recopy_connection", _fake_recopy_connection)
    await bridge._recopy_connection("aaa", "rchiodo-bigboy")
    assert "Recopy connection string failed" in capsys.readouterr().out


async def test_collector_bridge_cmd_recopy_no_paired_machines(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    server = _fake_server()
    bridge = CollectorConsoleBridge(server, asyncio.Event())
    await bridge.start()
    monkeypatch.setattr("pymonitor.server.remote_reporters_sync", lambda: [])
    bridge._cmd_recopy()
    assert "no paired machines" in capsys.readouterr().out


async def test_collector_bridge_cmd_recopy_lists_and_dispatches_selection(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    server = _fake_server()
    bridge = CollectorConsoleBridge(server, asyncio.Event())
    await bridge.start()
    reporters = [
        {"id": "aaa", "label": "rchiodo-bigboy"},
        {"id": "bbb", "label": "rchiodo-laptop"},
    ]
    monkeypatch.setattr("pymonitor.server.remote_reporters_sync", lambda: reporters)
    monkeypatch.setattr(cb, "input", lambda prompt="": "2", raising=False)
    recopied: list[tuple[str, str]] = []

    async def _fake_recopy(reporter_id: str, label: str) -> None:
        recopied.append((reporter_id, label))

    monkeypatch.setattr(bridge, "_recopy_connection", _fake_recopy)
    # _cmd_recopy blocks on future.result() for the valid-selection branch,
    # exactly like it does on its real background console thread -- so it
    # must run off the test's own event-loop thread here too, or the
    # scheduled `_recopy_connection` coroutine could never get a turn to run.
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, bridge._cmd_recopy)
    out = capsys.readouterr().out
    assert "rchiodo-bigboy" in out and "rchiodo-laptop" in out
    assert recopied == [("bbb", "rchiodo-laptop")]


async def test_collector_bridge_cmd_recopy_blank_choice_cancels(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    server = _fake_server()
    bridge = CollectorConsoleBridge(server, asyncio.Event())
    await bridge.start()
    reporters = [{"id": "aaa", "label": "rchiodo-bigboy"}]
    monkeypatch.setattr("pymonitor.server.remote_reporters_sync", lambda: reporters)
    monkeypatch.setattr(cb, "input", lambda prompt="": "", raising=False)
    mock_recopy = AsyncMock()
    monkeypatch.setattr(bridge, "_recopy_connection", mock_recopy)
    bridge._cmd_recopy()
    mock_recopy.assert_not_called()


async def test_collector_bridge_cmd_recopy_invalid_choice_prints_error(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    server = _fake_server()
    bridge = CollectorConsoleBridge(server, asyncio.Event())
    await bridge.start()
    reporters = [{"id": "aaa", "label": "rchiodo-bigboy"}]
    monkeypatch.setattr("pymonitor.server.remote_reporters_sync", lambda: reporters)
    monkeypatch.setattr(cb, "input", lambda prompt="": "9", raising=False)
    bridge._cmd_recopy()
    assert "Invalid selection" in capsys.readouterr().out


async def test_collector_bridge_cmd_stop_stops_server_and_sets_event() -> None:
    server = _fake_server()
    stop_event = asyncio.Event()
    bridge = CollectorConsoleBridge(server, stop_event)
    await bridge.start()
    bridge._cmd_stop()
    await asyncio.wait_for(stop_event.wait(), timeout=1)
    server.stop.assert_called_once()


async def test_collector_bridge_console_loop_dispatches_known_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _fake_server()
    bridge = CollectorConsoleBridge(server, asyncio.Event())
    await bridge.start()
    calls: list[str] = []
    monkeypatch.setattr(bridge, "_cmd_open", lambda: calls.append("open"))
    monkeypatch.setattr(bridge, "_cmd_test", lambda: calls.append("test"))
    lines = iter(["open", "TEST", "  ", "stop"])
    monkeypatch.setattr(cb, "input", lambda: next(lines), raising=False)
    monkeypatch.setattr(bridge, "_cmd_stop", lambda: calls.append("stop"))
    bridge._console_loop()
    assert calls == ["open", "test", "stop"]


async def test_collector_bridge_console_loop_unknown_command_prints_message(
    monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    server = _fake_server()
    bridge = CollectorConsoleBridge(server, asyncio.Event())
    await bridge.start()
    lines = iter(["bogus", "stop"])
    monkeypatch.setattr(cb, "input", lambda: next(lines), raising=False)
    monkeypatch.setattr(bridge, "_cmd_stop", lambda: None)
    bridge._console_loop()
    assert "Unknown command: 'bogus'" in capsys.readouterr().out


async def test_collector_bridge_push_theme_and_push_processes_marshal_onto_loop() -> None:
    server = _fake_server()
    bridge = CollectorConsoleBridge(server, asyncio.Event())
    await bridge.start()
    bridge._push_theme("dark")
    bridge._push_processes([{"pid": 1, "parentPid": None, "name": "copilot.exe", "startedAt": "x"}])
    await asyncio.sleep(0)
    server.on_theme.assert_called_once_with("dark")
    assert server.processes is not None
    assert server.processes["processes"][0]["name"] == "copilot.exe"


async def test_collector_bridge_on_power_triggers_server_power_event() -> None:
    server = _fake_server()
    bridge = CollectorConsoleBridge(server, asyncio.Event())
    await bridge.start()
    bridge._on_power("Resume")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    server.on_power_event.assert_called_once()


async def test_collector_bridge_shutdown_is_idempotent() -> None:
    server = _fake_server()
    bridge = CollectorConsoleBridge(server, asyncio.Event())
    await bridge.start()
    bridge._poll.stop = MagicMock()
    bridge._power.stop = MagicMock()
    bridge._shutdown()
    bridge._shutdown()
    bridge._poll.stop.assert_called_once()
    bridge._power.stop.assert_called_once()


async def test_collector_bridge_on_stop_shuts_down_and_sets_event() -> None:
    server = _fake_server()
    stop_event = asyncio.Event()
    bridge = CollectorConsoleBridge(server, stop_event)
    await bridge.start()
    bridge._poll.stop = MagicMock()
    bridge._power.stop = MagicMock()
    await bridge.on_stop()
    assert stop_event.is_set()
    bridge._poll.stop.assert_called_once()
    bridge._power.stop.assert_called_once()


async def test_collector_bridge_generate_connection_protocol_method(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _fake_server()
    bridge = CollectorConsoleBridge(server, asyncio.Event())
    await bridge.start()
    seen: list[str] = []

    async def _fake_generate(label: str) -> None:
        seen.append(label)

    monkeypatch.setattr(bridge, "_generate_connection", _fake_generate)
    await bridge.generate_connection(None)
    assert seen == [""]


# -- WatcherConsoleBridge -----------------------------------------------------


async def test_watcher_bridge_start_prints_header(capsys: Any) -> None:
    watcher = _fake_watcher()
    bridge = WatcherConsoleBridge(watcher, asyncio.Event())
    await bridge.start()
    assert "watcher" in capsys.readouterr().out.lower()


async def test_watcher_bridge_process_snapshot_reflects_last_poll() -> None:
    watcher = _fake_watcher()
    bridge = WatcherConsoleBridge(watcher, asyncio.Event())
    await bridge.start()
    assert bridge.process_snapshot() is None
    bridge._push_processes([{"pid": 5, "parentPid": None, "name": "github.exe", "startedAt": "x"}])
    snapshot = bridge.process_snapshot()
    assert snapshot is not None
    assert snapshot["processes"][0]["name"] == "github.exe"


async def test_watcher_bridge_on_health_changed_is_noop() -> None:
    watcher = _fake_watcher()
    bridge = WatcherConsoleBridge(watcher, asyncio.Event())
    await bridge.start()
    assert await bridge.on_health_changed({"ok": False}) is None


async def test_watcher_bridge_cmd_status_not_paired(capsys: Any) -> None:
    watcher = _fake_watcher()
    watcher.pairing = None
    bridge = WatcherConsoleBridge(watcher, asyncio.Event())
    await bridge.start()
    bridge._cmd_status()
    assert "Not paired yet" in capsys.readouterr().out


async def test_watcher_bridge_cmd_status_paired(capsys: Any) -> None:
    watcher = _fake_watcher()
    watcher.pairing = {"label": "sub1"}
    watcher.health = {"healthy": True, "issue": None}
    bridge = WatcherConsoleBridge(watcher, asyncio.Event())
    await bridge.start()
    bridge._cmd_status()
    out = capsys.readouterr().out
    assert "sub1" in out and "healthy=True" in out


async def test_watcher_bridge_cmd_connect_blank_cancels(monkeypatch: pytest.MonkeyPatch, capsys: Any) -> None:
    watcher = _fake_watcher()
    bridge = WatcherConsoleBridge(watcher, asyncio.Event())
    await bridge.start()
    monkeypatch.setattr(cb, "input", lambda prompt="": "", raising=False)
    bridge._cmd_connect()
    assert "Cancelled" in capsys.readouterr().out
    watcher.apply_connection_string.assert_not_called()


async def test_watcher_bridge_apply_connection_success_prints_result(capsys: Any) -> None:
    watcher = _fake_watcher()
    watcher.apply_connection_string.return_value = {"host": "HOSTPC", "label": "sub1"}
    bridge = WatcherConsoleBridge(watcher, asyncio.Event())
    await bridge.start()
    await bridge._apply_connection("csm1:abc")
    watcher.apply_connection_string.assert_called_once_with("csm1:abc")
    assert "Connected to HOSTPC as sub1" in capsys.readouterr().out


async def test_watcher_bridge_apply_connection_failure_prints_error(capsys: Any) -> None:
    watcher = _fake_watcher()
    watcher.apply_connection_string.side_effect = RuntimeError("bad string")
    bridge = WatcherConsoleBridge(watcher, asyncio.Event())
    await bridge.start()
    await bridge._apply_connection("csm1:bad")
    assert "Connection failed" in capsys.readouterr().out


async def test_watcher_bridge_cmd_stop_stops_watcher_and_sets_event() -> None:
    watcher = _fake_watcher()
    stop_event = asyncio.Event()
    bridge = WatcherConsoleBridge(watcher, stop_event)
    await bridge.start()
    bridge._cmd_stop()
    await asyncio.wait_for(stop_event.wait(), timeout=1)
    watcher.stop.assert_called_once()


async def test_watcher_bridge_on_power_resets_watcher_and_clears_processes() -> None:
    watcher = _fake_watcher()
    bridge = WatcherConsoleBridge(watcher, asyncio.Event())
    await bridge.start()
    bridge._push_processes([{"pid": 1, "parentPid": None, "name": "copilot.exe", "startedAt": "x"}])
    bridge._on_power("Resume")
    assert watcher.reset is not None
    assert bridge.process_snapshot() is None


async def test_watcher_bridge_on_stop_shuts_down_and_sets_event() -> None:
    watcher = _fake_watcher()
    stop_event = asyncio.Event()
    bridge = WatcherConsoleBridge(watcher, stop_event)
    await bridge.start()
    bridge._poll.stop = MagicMock()
    bridge._power.stop = MagicMock()
    await bridge.on_stop()
    assert stop_event.is_set()
    bridge._poll.stop.assert_called_once()
    bridge._power.stop.assert_called_once()


async def test_watcher_bridge_shutdown_is_idempotent() -> None:
    watcher = _fake_watcher()
    bridge = WatcherConsoleBridge(watcher, asyncio.Event())
    await bridge.start()
    bridge._poll.stop = MagicMock()
    bridge._power.stop = MagicMock()
    bridge._shutdown()
    bridge._shutdown()
    bridge._poll.stop.assert_called_once()
    bridge._power.stop.assert_called_once()


# -- _PollWorker / _read_theme / _snapshot_processes (ported unchanged) ------


def test_poll_worker_tick_calls_push_theme_and_processes() -> None:
    themes: list[str | None] = []
    processes: list[list[dict[str, Any]]] = []
    worker = cb._PollWorker(themes.append, processes.append)
    worker.tick()
    assert len(themes) == 1
    assert len(processes) == 1


def test_poll_worker_tick_swallows_theme_exception() -> None:
    def _boom(mode: Any) -> None:
        raise RuntimeError("theme push failed")

    processes: list[Any] = []
    worker = cb._PollWorker(_boom, processes.append)
    worker.tick()  # must not raise
    assert processes == []  # push_processes never reached since push_theme raised first


def test_read_theme_returns_none_when_winreg_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cb, "winreg", None)
    assert cb._read_theme() is None


def test_read_theme_maps_registry_value(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_winreg = MagicMock()
    fake_winreg.HKEY_CURRENT_USER = 1
    fake_winreg.QueryValueEx.return_value = (1, 4)
    fake_winreg.OpenKey.return_value.__enter__.return_value = MagicMock()
    monkeypatch.setattr(cb, "winreg", fake_winreg)
    assert cb._read_theme() == "light"

    fake_winreg.QueryValueEx.return_value = (0, 4)
    assert cb._read_theme() == "dark"


def test_read_theme_returns_none_on_os_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_winreg = MagicMock()
    fake_winreg.OpenKey.side_effect = OSError("missing key")
    monkeypatch.setattr(cb, "winreg", fake_winreg)
    assert cb._read_theme() is None


def test_snapshot_processes_filters_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeProc:
        def __init__(self, info: dict[str, Any]) -> None:
            self.info = info

    fake_procs = [
        _FakeProc({"pid": 1, "ppid": 0, "name": "copilot.exe", "create_time": time.time()}),
        _FakeProc({"pid": 2, "ppid": 0, "name": "notepad.exe", "create_time": time.time()}),
        _FakeProc({"pid": 3, "ppid": 1, "name": "github.exe", "create_time": time.time()}),
    ]
    monkeypatch.setattr(cb.psutil, "process_iter", lambda attrs: fake_procs)
    snapshot = cb._snapshot_processes()
    names = {entry["name"] for entry in snapshot}
    assert names == {"copilot.exe", "github.exe"}


def test_snapshot_processes_skips_vanished_process(monkeypatch: pytest.MonkeyPatch) -> None:
    class _VanishingProc:
        @property
        def info(self) -> dict[str, Any]:
            raise cb.psutil.NoSuchProcess(pid=1)

    monkeypatch.setattr(cb.psutil, "process_iter", lambda attrs: [_VanishingProc()])
    assert cb._snapshot_processes() == []
