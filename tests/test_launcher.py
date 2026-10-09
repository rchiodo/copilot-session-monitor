"""Tests for pymonitor.launcher (port of scripts/Start-Role.ps1 and
scripts/Stop-Role.ps1's testable behavior).

Every test injects fake HTTP/spawn/sleep callables so none of this touches
a real network socket or process -- the behavior under test is the launch/
stop *orchestration logic* (live-instance detection, safety checks, polling
bounds), not the transport.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from pymonitor.launcher import (
    COLLECTOR_ROLE,
    WATCHER_ROLE,
    LauncherError,
    data_dir_for,
    ensure_lan_bind,
    get_live_role,
    load_runtime,
    probe_status,
    stop_role,
)


def _write_runtime(data_dir: Path, role, **overrides: Any) -> dict[str, Any]:
    import json

    data_dir.mkdir(parents=True, exist_ok=True)
    runtime = {
        "pid": 1234,
        "instanceId": "11111111-1111-1111-1111-111111111111",
        "url": "http://127.0.0.1:43187",
        "token": "deadbeef",
        **overrides,
    }
    (data_dir / role.runtime_file).write_text(json.dumps(runtime), encoding="utf-8")
    return runtime


# --- data_dir_for -----------------------------------------------------


def test_data_dir_for_prefers_monitor_data_dir_env_var(tmp_path: Path) -> None:
    assert data_dir_for(tmp_path, env={"MONITOR_DATA_DIR": str(tmp_path / "custom")}) == tmp_path / "custom"


def test_data_dir_for_falls_back_to_dot_local_under_root(tmp_path: Path) -> None:
    assert data_dir_for(tmp_path, env={}) == tmp_path / ".local"


# --- load_runtime / probe_status / get_live_role ---------------------------


def test_load_runtime_returns_none_when_the_runtime_file_is_absent(tmp_path: Path) -> None:
    assert load_runtime(tmp_path, COLLECTOR_ROLE) is None


def test_load_runtime_rejects_a_non_loopback_url(tmp_path: Path) -> None:
    _write_runtime(tmp_path, COLLECTOR_ROLE, url="http://10.0.0.5:43187")
    with pytest.raises(LauncherError):
        load_runtime(tmp_path, COLLECTOR_ROLE)


def test_probe_status_returns_none_on_a_connection_failure(tmp_path: Path) -> None:
    runtime = _write_runtime(tmp_path, COLLECTOR_ROLE)

    def _raises(url: str, timeout: float) -> dict[str, Any]:
        raise OSError("connection refused")

    assert probe_status(runtime, COLLECTOR_ROLE, http_get_json=_raises) is None


def test_get_live_role_matches_when_the_response_instance_id_agrees(tmp_path: Path) -> None:
    runtime = _write_runtime(tmp_path, COLLECTOR_ROLE)

    def _ok(url: str, timeout: float) -> dict[str, Any]:
        return {"instanceId": runtime["instanceId"], "healthy": True}

    assert get_live_role(tmp_path, COLLECTOR_ROLE, http_get_json=_ok) == runtime


def test_get_live_role_rejects_a_stale_runtime_file_describing_a_different_process(tmp_path: Path) -> None:
    # This is the exact "stale runtime file" case Get-LiveRole guards
    # against: a process died uncleanly (or a new unrelated process is now
    # listening on the recorded port) and the live instanceId no longer
    # matches what the runtime file recorded.
    _write_runtime(tmp_path, COLLECTOR_ROLE)

    def _mismatch(url: str, timeout: float) -> dict[str, Any]:
        return {"instanceId": "different-instance", "healthy": True}

    assert get_live_role(tmp_path, COLLECTOR_ROLE, http_get_json=_mismatch) is None


def test_get_live_role_returns_none_when_no_runtime_file_exists(tmp_path: Path) -> None:
    assert get_live_role(tmp_path, COLLECTOR_ROLE, http_get_json=lambda *a: {}) is None


# --- stop_role -----------------------------------------------------------


def test_stop_role_is_a_no_op_when_no_runtime_file_exists(tmp_path: Path) -> None:
    assert stop_role(tmp_path, COLLECTOR_ROLE, data_dir=tmp_path, http_get_json=lambda *a: {}, http_post=lambda *a: None) is False


def test_stop_role_rejects_a_non_loopback_url(tmp_path: Path) -> None:
    _write_runtime(tmp_path, COLLECTOR_ROLE, url="http://192.168.1.5:43187")
    with pytest.raises(LauncherError):
        stop_role(tmp_path, COLLECTOR_ROLE, data_dir=tmp_path, http_get_json=lambda *a: {}, http_post=lambda *a: None)


def test_stop_role_refuses_to_stop_an_instance_id_mismatch(tmp_path: Path) -> None:
    # The critical safety check: refuse to stop a process that happens to
    # be listening on the recorded port but isn't the one the runtime file
    # describes.
    runtime = _write_runtime(tmp_path, COLLECTOR_ROLE)

    def _mismatch(url: str, timeout: float) -> dict[str, Any]:
        return {"instanceId": "not-" + runtime["instanceId"]}

    with pytest.raises(LauncherError, match="Instance mismatch"):
        stop_role(tmp_path, COLLECTOR_ROLE, data_dir=tmp_path, http_get_json=_mismatch, http_post=lambda *a: None)


def test_stop_role_posts_a_bearer_token_matching_the_runtime_file(tmp_path: Path) -> None:
    runtime = _write_runtime(tmp_path, COLLECTOR_ROLE)
    posted = []

    def _ok(url: str, timeout: float) -> dict[str, Any]:
        return {"instanceId": runtime["instanceId"]}

    def _post(url: str, headers: dict[str, str], timeout: float) -> None:
        posted.append((url, headers))
        (tmp_path / COLLECTOR_ROLE.runtime_file).unlink()

    assert stop_role(tmp_path, COLLECTOR_ROLE, data_dir=tmp_path, http_get_json=_ok, http_post=_post, sleep=lambda _s: None) is True
    [(url, headers)] = posted
    assert url == f"{runtime['url']}{COLLECTOR_ROLE.stop_endpoint}"
    assert headers == {"Authorization": f"Bearer {runtime['token']}"}


def _fallback_http_get_json(
    instance_id: str, token: str, url: str = "http://127.0.0.1:43187"
) -> Any:
    """A fake transport that answers the collector's two unauthenticated
    discovery endpoints (status + control-token) at its well-known URL,
    simulating a live collector with no runtime file on disk."""

    def _get(target_url: str, timeout: float) -> dict[str, Any]:
        if target_url == f"{url}/api/status":
            return {"instanceId": instance_id, "healthy": True}
        if target_url == f"{url}/api/control":
            return {"token": token}
        raise OSError(f"unexpected url {target_url}")

    return _get


def test_get_live_role_reconstructs_a_live_collector_when_the_runtime_file_is_missing(
    tmp_path: Path,
) -> None:
    # The core fix: runtime.json is simply absent (lost, or never written
    # at startup for whatever reason) but the collector is still alive and
    # reachable on its fixed, deterministic dashboard port -- so this must
    # not be treated as "not running".
    instance_id = "88888888-8888-8888-8888-888888888888"
    runtime = get_live_role(
        tmp_path, COLLECTOR_ROLE, http_get_json=_fallback_http_get_json(instance_id, "feedface")
    )
    assert runtime is not None
    assert runtime["instanceId"] == instance_id
    assert runtime["token"] == "feedface"
    assert runtime["url"] == "http://127.0.0.1:43187"


def test_get_live_role_fallback_is_a_no_op_for_the_watcher_role(tmp_path: Path) -> None:
    # The watcher has no well-known port (OS-assigned/ephemeral), so even a
    # transport that would happily answer any request must not be used to
    # fabricate a watcher runtime -- a missing watcher-runtime.json always
    # means "not running".
    assert (
        get_live_role(
            tmp_path, WATCHER_ROLE, http_get_json=_fallback_http_get_json("anything", "anything")
        )
        is None
    )


def test_get_live_role_fallback_returns_none_when_the_collector_is_truly_not_running(
    tmp_path: Path,
) -> None:
    def _refused(url: str, timeout: float) -> dict[str, Any]:
        raise OSError("connection refused")

    assert get_live_role(tmp_path, COLLECTOR_ROLE, http_get_json=_refused) is None


def test_stop_role_stops_a_collector_discovered_via_fallback_when_runtime_file_is_missing(
    tmp_path: Path,
) -> None:
    instance_id = "99999999-9999-9999-9999-999999999999"
    get_json = _fallback_http_get_json(instance_id, "feedface")
    posted = []
    still_alive = [True]

    def _post(url: str, headers: dict[str, str], timeout: float) -> None:
        posted.append((url, headers))
        still_alive[0] = False

    def _get(url: str, timeout: float) -> dict[str, Any]:
        if not still_alive[0]:
            raise OSError("connection refused")
        return get_json(url, timeout)

    assert (
        stop_role(tmp_path, COLLECTOR_ROLE, data_dir=tmp_path, http_get_json=_get, http_post=_post, sleep=lambda _s: None)
        is True
    )
    [(url, headers)] = posted
    assert url == "http://127.0.0.1:43187/api/stop"
    assert headers == {"Authorization": "Bearer feedface"}


def test_stop_role_remains_a_no_op_when_the_collector_is_truly_not_running_and_no_file_exists(
    tmp_path: Path,
) -> None:
    def _refused(url: str, timeout: float) -> dict[str, Any]:
        raise OSError("connection refused")

    assert (
        stop_role(tmp_path, COLLECTOR_ROLE, data_dir=tmp_path, http_get_json=_refused, http_post=lambda *a: None)
        is False
    )


def test_stop_role_raises_if_the_runtime_file_never_disappears(tmp_path: Path) -> None:
    runtime = _write_runtime(tmp_path, COLLECTOR_ROLE)

    def _ok(url: str, timeout: float) -> dict[str, Any]:
        return {"instanceId": runtime["instanceId"]}

    with pytest.raises(LauncherError, match="did not finish stopping"):
        stop_role(
            tmp_path,
            COLLECTOR_ROLE,
            data_dir=tmp_path,
            http_get_json=_ok,
            http_post=lambda *a: None,
            sleep=lambda _s: None,
            poll_attempts=3,
        )


# --- ensure_lan_bind -------------------------------------------------------


def test_ensure_lan_bind_stops_a_running_collector_before_reconfiguring(tmp_path: Path) -> None:
    runtime = _write_runtime(tmp_path, COLLECTOR_ROLE)
    posted = []

    def _get(url: str, timeout: float) -> dict[str, Any]:
        return {"instanceId": runtime["instanceId"]}

    def _post(url: str, headers: dict[str, str], timeout: float) -> None:
        posted.append(url)
        (tmp_path / COLLECTOR_ROLE.runtime_file).unlink()

    commands: list[list[str]] = []

    address = ensure_lan_bind(
        tmp_path,
        port=43188,
        data_dir=tmp_path,
        http_get_json=_get,
        http_post=_post,
        sleep=lambda _seconds: None,
        detect_lan_address=lambda: "192.168.1.42",
        run_configuration_command=commands.append,
    )

    assert address == "192.168.1.42"
    assert posted, "the already-running collector should have been stopped first"
    assert commands == [["initialize", "192.168.1.42", "43188", "replace"]]


def test_ensure_lan_bind_tolerates_no_collector_currently_running(tmp_path: Path) -> None:
    # stop_role is a no-op (returns False) when nothing is running -- this
    # must not stop ensure_lan_bind from proceeding to detect + initialize.
    commands: list[list[str]] = []

    address = ensure_lan_bind(
        tmp_path,
        data_dir=tmp_path,
        http_get_json=lambda *a: {},
        http_post=lambda *a: None,
        sleep=lambda _seconds: None,
        detect_lan_address=lambda: "10.0.0.7",
        run_configuration_command=commands.append,
    )

    assert address == "10.0.0.7"
    assert commands == [["initialize", "10.0.0.7", "43188", "replace"]]


def test_ensure_lan_bind_passes_the_requested_port_to_the_configuration_command(tmp_path: Path) -> None:
    commands: list[list[str]] = []

    ensure_lan_bind(
        tmp_path,
        port=9000,
        data_dir=tmp_path,
        http_get_json=lambda *a: {},
        http_post=lambda *a: None,
        sleep=lambda _seconds: None,
        detect_lan_address=lambda: "10.0.0.9",
        run_configuration_command=commands.append,
    )

    assert commands == [["initialize", "10.0.0.9", "9000", "replace"]]


def test_ensure_lan_bind_is_a_no_op_when_already_bound_to_the_same_address_and_port(
    tmp_path: Path,
) -> None:
    # Regression test: re-running `--lan` against an already-correct bind
    # address/port must not stop the collector or call "initialize ...
    # replace" -- that would unconditionally rotate the TLS certificate
    # (configuration.initialize) and silently break every already-paired
    # remote watcher's certificate pin for no reason.
    (tmp_path / "collector.json").write_text(
        '{"bindAddress": "192.168.1.42", "port": 43188}', encoding="utf-8"
    )
    calls: list[str] = []

    def _get(url: str, timeout: float) -> dict[str, Any]:
        calls.append("get")
        return {}

    def _post(url: str, headers: dict[str, str], timeout: float) -> None:
        calls.append("post")

    commands: list[list[str]] = []

    address = ensure_lan_bind(
        tmp_path,
        port=43188,
        data_dir=tmp_path,
        http_get_json=_get,
        http_post=_post,
        sleep=lambda _seconds: None,
        detect_lan_address=lambda: "192.168.1.42",
        run_configuration_command=commands.append,
    )

    assert address == "192.168.1.42"
    assert calls == [], "stop_role must not be consulted when nothing is changing"
    assert commands == [], "initialize/reconfigure must not run when nothing is changing"


def test_ensure_lan_bind_reconfigures_when_address_differs_from_existing_config(
    tmp_path: Path,
) -> None:
    (tmp_path / "collector.json").write_text(
        '{"bindAddress": "10.0.0.1", "port": 43188}', encoding="utf-8"
    )
    commands: list[list[str]] = []

    address = ensure_lan_bind(
        tmp_path,
        port=43188,
        data_dir=tmp_path,
        http_get_json=lambda *a: {},
        http_post=lambda *a: None,
        sleep=lambda _seconds: None,
        detect_lan_address=lambda: "192.168.1.42",
        run_configuration_command=commands.append,
    )

    assert address == "192.168.1.42"
    assert commands == [["initialize", "192.168.1.42", "43188", "replace"]]


def test_ensure_lan_bind_defaults_data_dir_to_data_dir_for_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # No data_dir override supplied: ensure_lan_bind should derive it the
    # same way stop_role does, via data_dir_for(root), rather than
    # requiring every caller to pass it explicitly.
    monkeypatch.delenv("MONITOR_DATA_DIR", raising=False)
    commands: list[list[str]] = []

    address = ensure_lan_bind(
        tmp_path,
        http_get_json=lambda *a: {},
        http_post=lambda *a: None,
        sleep=lambda _seconds: None,
        detect_lan_address=lambda: "10.0.0.11",
        run_configuration_command=commands.append,
    )

    assert address == "10.0.0.11"
    assert commands == [["initialize", "10.0.0.11", "43188", "replace"]]
