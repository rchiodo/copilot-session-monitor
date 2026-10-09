"""Testable launcher logic behind the PEP 723 standalone scripts.

Ports the remaining non-spawn behavior of ``scripts/Stop-Role.ps1`` (and,
by extension, the wrapper scripts that used to call into it:
``Stop-Host.ps1``, ``Stop-Client.ps1``) into importable, unit-testable
functions, plus the runtime-file discovery helpers shared by the stop
scripts and the ``--lan`` reconfiguration path. Startup is no longer a
detached background spawn: ``start-host.py``/``start-client.py``/
``start-tray.py`` now call ``pymonitor.cli``'s ``host_main``/``client_main``
directly and run attached to the console.

Every HTTP call and sleep is injectable so tests never need a real
collector/watcher process or network socket. Defaults use the stdlib
(``urllib.request``) so the PEP 723 scripts need no dependency beyond the
editable ``pymonitor`` package itself.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

__all__ = [
    "LauncherError",
    "RoleConfig",
    "COLLECTOR_ROLE",
    "WATCHER_ROLE",
    "ROLES",
    "data_dir_for",
    "load_runtime",
    "probe_status",
    "get_live_role",
    "stop_role",
    "ensure_lan_bind",
]

# Mirrors Stop-Role.ps1's URL validation: refuse to trust/act on a runtime
# file whose recorded URL isn't a loopback HTTP URL -- this is the guard
# that prevents a stale/tampered runtime file from redirecting a stop/health
# request at an arbitrary host.
_LOOPBACK_URL_RE = re.compile(r"^http://127\.0\.0\.1:\d+$")


class LauncherError(RuntimeError):
    """Raised for the same fatal conditions the .ps1 scripts `throw` on."""


@dataclass(frozen=True)
class RoleConfig:
    """Everything that differs between the collector and watcher roles."""

    name: str  # "collector" | "watcher"
    cli_entry: str  # `python -m pymonitor.cli <cli_entry>` -- "host" | "client"
    runtime_file: str  # "runtime.json" | "watcher-runtime.json"
    status_endpoint: str  # "/api/status" | "/status"
    stop_endpoint: str  # "/api/stop" | "/stop"
    # Collector-only: its dashboard binds a fixed (MONITOR_PORT-overridable)
    # loopback port, so a live instance can be rediscovered over HTTP even
    # if its runtime file is missing or was never written. The watcher binds
    # an OS-assigned ephemeral port and has no well-known address, so it
    # leaves these unset and gets none of this fallback.
    default_url_candidates: tuple[str, ...] = ()
    control_endpoint: str | None = None  # unauthenticated token-fetch endpoint


COLLECTOR_ROLE = RoleConfig(
    "collector", "host", "runtime.json", "/api/status", "/api/stop",
    default_url_candidates=(f"http://127.0.0.1:{os.environ.get('MONITOR_PORT', '43187')}",),
    control_endpoint="/api/control",
)
WATCHER_ROLE = RoleConfig("watcher", "client", "watcher-runtime.json", "/status", "/stop")
ROLES = {"collector": COLLECTOR_ROLE, "watcher": WATCHER_ROLE}


def data_dir_for(root: Path, env: dict[str, str] | None = None) -> Path:
    """Port of Start-Role.ps1's `$data` resolution: `$env:MONITOR_DATA_DIR`
    or `<repo root>/.local`."""
    env = os.environ if env is None else env
    configured = env.get("MONITOR_DATA_DIR")
    return Path(configured) if configured else root / ".local"


def _validate_url(url: str) -> None:
    if not _LOOPBACK_URL_RE.match(url or ""):
        raise LauncherError("Runtime file does not reference a loopback URL; refusing to proceed.")


def load_runtime(data_dir: Path, role: RoleConfig) -> dict[str, Any] | None:
    """Read and minimally validate `runtime.json`/`watcher-runtime.json`.
    Returns None if the file doesn't exist (not running, or already
    stopped) -- never an error, matching both .ps1 scripts' `Test-Path`
    early-outs."""
    try:
        raw = (data_dir / role.runtime_file).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    runtime = json.loads(raw)
    _validate_url(runtime.get("url", ""))
    return runtime


HttpGetJson = Callable[[str, float], dict[str, Any]]
HttpPost = Callable[[str, dict[str, str], float], None]


def _default_http_get_json(url: str, timeout: float) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - loopback only
        return json.loads(response.read().decode("utf-8"))


def _default_http_post(url: str, headers: dict[str, str], timeout: float) -> None:
    request = urllib.request.Request(url, method="POST", headers=headers, data=b"")
    with urllib.request.urlopen(request, timeout=timeout):  # noqa: S310 - loopback only
        pass


def probe_status(
    runtime: dict[str, Any],
    role: RoleConfig,
    *,
    timeout: float = 2.0,
    http_get_json: HttpGetJson = _default_http_get_json,
) -> dict[str, Any] | None:
    """Port of Get-LiveRole's probe: any connection failure (refused,
    timed out, non-JSON) just means "not live", not an error."""
    try:
        return http_get_json(f"{runtime['url']}{role.status_endpoint}", timeout)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def _reconstruct_live_runtime(
    role: RoleConfig,
    *,
    timeout: float,
    http_get_json: HttpGetJson,
) -> dict[str, Any] | None:
    """Fallback for when the runtime file is missing -- lost to an external
    process, or never written due to some startup edge case -- but the role
    is still alive and serving on its well-known loopback port. Only does
    anything for roles that declare `default_url_candidates`/`control_endpoint`
    (collector); for roles without those (watcher), this is always a no-op,
    preserving the original "missing file means not running" behavior."""
    if not role.default_url_candidates or not role.control_endpoint:
        return None
    for url in role.default_url_candidates:
        try:
            status = http_get_json(f"{url}{role.status_endpoint}", timeout)
            instance_id = status.get("instanceId")
            if not instance_id:
                continue
            control = http_get_json(f"{url}{role.control_endpoint}", timeout)
            token = control.get("token")
            if not token:
                continue
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            continue
        return {"pid": None, "instanceId": instance_id, "url": url, "token": token}
    return None


def get_live_role(
    data_dir: Path,
    role: RoleConfig,
    *,
    timeout: float = 2.0,
    http_get_json: HttpGetJson = _default_http_get_json,
) -> dict[str, Any] | None:
    """Port of Get-LiveRole: a runtime file only counts as describing a
    *live, matching-identity* process if that process's own `/api/status`
    (or `/status`) response echoes back the same `instanceId` recorded in
    the runtime file. A stale runtime file left behind by a process that
    died uncleanly, or one now describing an unrelated process that
    happens to be listening on the same port, is correctly treated as
    "not live". If the runtime file doesn't exist at all, falls back to
    `_reconstruct_live_runtime` (collector only) rather than assuming the
    role isn't running."""
    runtime = load_runtime(data_dir, role)
    if runtime is None:
        return _reconstruct_live_runtime(role, timeout=timeout, http_get_json=http_get_json)
    status = probe_status(runtime, role, timeout=timeout, http_get_json=http_get_json)
    if status is not None and status.get("instanceId") == runtime.get("instanceId"):
        return runtime
    return None


def stop_role(
    root: Path,
    role: RoleConfig,
    *,
    data_dir: Path | None = None,
    http_get_json: HttpGetJson = _default_http_get_json,
    http_post: HttpPost = _default_http_post,
    sleep: Callable[[float], None] = time.sleep,
    status_timeout: float = 3.0,
    stop_timeout: float = 3.0,
    poll_attempts: int = 60,
    poll_interval: float = 0.25,
) -> bool:
    """Port of Stop-Role.ps1. Returns False if no runtime file was present
    *and* the role couldn't be rediscovered live (a no-op, not an error --
    the role simply isn't running). Returns True once the role has
    confirmed-stopped.

    Raises `LauncherError` if the runtime file's recorded `instanceId`
    doesn't match what the live process reports -- the safety check that
    refuses to stop an unrelated process that happens to be listening on
    the recorded port -- or if the role never finishes stopping in time.
    """
    data_dir = data_dir if data_dir is not None else data_dir_for(root)
    runtime_path = data_dir / role.runtime_file
    had_runtime_file = True
    try:
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        had_runtime_file = False
        runtime = _reconstruct_live_runtime(role, timeout=status_timeout, http_get_json=http_get_json)
        if runtime is None:
            return False
    if had_runtime_file:
        _validate_url(runtime.get("url", ""))

    status = http_get_json(f"{runtime['url']}{role.status_endpoint}", status_timeout)
    if status.get("instanceId") != runtime.get("instanceId"):
        raise LauncherError("Instance mismatch; refusing to stop another process.")

    http_post(
        f"{runtime['url']}{role.stop_endpoint}",
        {"Authorization": f"Bearer {runtime['token']}"},
        stop_timeout,
    )

    if had_runtime_file:
        for _ in range(poll_attempts):
            if not runtime_path.exists():
                break
            sleep(poll_interval)
        if runtime_path.exists():
            raise LauncherError(f"{role.name} did not finish stopping.")
    else:
        # No runtime file to watch disappear (it was never there) -- poll
        # the reconstructed URL instead, until it stops responding.
        for _ in range(poll_attempts):
            if probe_status(runtime, role, timeout=status_timeout, http_get_json=http_get_json) is None:
                break
            sleep(poll_interval)
        else:
            raise LauncherError(f"{role.name} did not finish stopping.")
    return True


ConfigurationCommandFn = Callable[[list[str]], None]
DetectLanAddressFn = Callable[[], str]


def _default_detect_lan_address() -> str:
    from .configuration import detect_lan_address

    return detect_lan_address()


def _default_run_configuration_command(command: list[str]) -> None:
    import asyncio

    from .configuration import configuration_command

    asyncio.run(configuration_command(command))


def ensure_lan_bind(
    root: Path,
    *,
    port: int = 43188,
    data_dir: Path | None = None,
    http_get_json: HttpGetJson = _default_http_get_json,
    http_post: HttpPost = _default_http_post,
    sleep: Callable[[float], None] = time.sleep,
    detect_lan_address: DetectLanAddressFn = _default_detect_lan_address,
    run_configuration_command: ConfigurationCommandFn = _default_run_configuration_command,
) -> str:
    """Collapse the manual "stop, detect, reconfigure" multi-machine setup
    dance (``stop-host.py`` then ``detect-lan-ip.py``/``init-host.py
    --reconfigure``) into the one step ``start-host.py --lan``/
    ``start-tray.py --host --lan`` needs before starting a collector that
    should be LAN-reachable instead of loopback-only.

    Stops any live collector first -- `initialize()` refuses to run while
    one owns the data directory -- then detects this machine's LAN-facing
    address (`configuration.detect_lan_address`) and applies it via the
    same ``initialize ... replace`` path ``init-host.py --reconfigure`` /
    ``detect-lan-ip.py`` use. A no-op `stop_role` (collector wasn't running)
    is not an error. Returns the address that was applied.

    If the collector is already configured for this exact LAN address and
    port, the stop+reconfigure round trip is skipped entirely and this is a
    true no-op: ``initialize(..., reconfigure=True)`` unconditionally
    rotates the TLS certificate (see `configuration.initialize`), which
    would otherwise silently invalidate every already-paired remote
    watcher's pinned certificate (see `protocol.request`'s pin check) each
    time ``--lan`` is re-run, even when nothing actually changed.

    This is only ever invoked explicitly (e.g. a ``--lan`` flag), never
    automatically when starting a collector: a fresh collector binding
    loopback-only by default -- no LAN interface opened -- is a deliberate
    privacy posture that must stay opt-in.
    """
    data_dir = data_dir if data_dir is not None else data_dir_for(root)
    address = detect_lan_address()

    try:
        existing = json.loads((data_dir / "collector.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        existing = None
    if existing is not None and existing.get("bindAddress") == address and existing.get("port") == port:
        return address

    stop_role(
        root,
        COLLECTOR_ROLE,
        data_dir=data_dir,
        http_get_json=http_get_json,
        http_post=http_post,
        sleep=sleep,
    )
    run_configuration_command(["initialize", address, str(port), "replace"])
    return address
