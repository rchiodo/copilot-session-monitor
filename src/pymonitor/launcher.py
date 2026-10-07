"""Testable launcher logic behind the PEP 723 standalone scripts.

Ports the real behavior of ``scripts/Start-Role.ps1`` and
``scripts/Stop-Role.ps1`` (and, by extension, the six thin root-level
wrappers that used to call into them: ``Initialize-Host.ps1``,
``Start-Host-Headless.ps1``, ``Start-Client-Headless.ps1``,
``Start-Tray.ps1``, ``Stop-Host.ps1``, ``Stop-Client.ps1``) into importable,
unit-testable functions. The two PowerShell "role" scripts had no direct
user-facing entry point of their own -- they were only ever invoked by the
six wrapper scripts -- so their logic is absorbed into this module rather
than becoming two more standalone scripts; the replacement PEP 723 scripts
(``start-host.py``, ``start-client.py``, ``start-tray.py``, ``stop-host.py``,
``stop-client.py``, ``init-host.py``) are thin argument-parsing wrappers
around the functions here.

Every HTTP call, spawn, and sleep is injectable so tests never need a real
collector/watcher process or network socket. Defaults use the stdlib
(``urllib.request``) and ``subprocess.Popen`` so the PEP 723 scripts need no
dependency beyond the editable ``pymonitor`` package itself.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
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
    "StartResult",
    "data_dir_for",
    "check_python_version",
    "load_runtime",
    "probe_status",
    "get_live_role",
    "start_role",
    "stop_role",
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


@dataclass
class StartResult:
    """What `start_role` learned once a role was confirmed running."""

    runtime: dict[str, Any]
    status: dict[str, Any]
    paired_pending: bool  # watcher-only: started but not yet paired with a host


def data_dir_for(root: Path, env: dict[str, str] | None = None) -> Path:
    """Port of Start-Role.ps1's `$data` resolution: `$env:MONITOR_DATA_DIR`
    or `<repo root>/.local`."""
    env = os.environ if env is None else env
    configured = env.get("MONITOR_DATA_DIR")
    return Path(configured) if configured else root / ".local"


def check_python_version(
    minimum: tuple[int, int] = (3, 11), *, version_info: tuple[int, int] | None = None
) -> None:
    """Port of Start-Role.ps1's `python -c "import sys; ..."` version gate.
    Less load-bearing now that `uv run` pins the interpreter per the
    script's `requires-python`, but kept as the same sanity check."""
    actual = version_info if version_info is not None else (sys.version_info[0], sys.version_info[1])
    if actual < minimum:
        raise LauncherError(
            f"Python {minimum[0]}.{minimum[1]} or newer is required; found {actual[0]}.{actual[1]}."
        )


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


SpawnFn = Callable[..., "subprocess.Popen[bytes]"]


def _default_spawn(args: list[str], *, cwd: str, stdout_path: Path, stderr_path: Path) -> "subprocess.Popen[bytes]":
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    stdout_file = stdout_path.open("ab")
    stderr_file = stderr_path.open("ab")
    try:
        return subprocess.Popen(
            args,
            cwd=cwd,
            stdout=stdout_file,
            stderr=stderr_file,
            creationflags=creationflags,
            close_fds=True,
        )
    finally:
        # Popen has already duplicated the handles into the child; the
        # parent-side file objects can be closed immediately (same pattern
        # Start-Role.ps1 relied on via `-RedirectStandardOutput`/`-Error`,
        # which likewise doesn't keep the PowerShell-side handle open).
        stdout_file.close()
        stderr_file.close()


def start_role(
    root: Path,
    role: RoleConfig,
    *,
    no_browser: bool = False,
    data_dir: Path | None = None,
    python_executable: str | None = None,
    http_get_json: HttpGetJson = _default_http_get_json,
    spawn: SpawnFn = _default_spawn,
    sleep: Callable[[float], None] = time.sleep,
    open_browser: Callable[[str], None] | None = None,
    startup_attempts: int = 60,
    startup_interval: float = 0.5,
    health_attempts: int = 60,
    health_timeout: float = 3.0,
) -> StartResult:
    """Port of Start-Role.ps1. Reuses an already-live, matching-identity
    instance; otherwise spawns `python -m pymonitor.cli {host|client}` as a
    hidden background process and polls for it to come up healthy.

    Raises `LauncherError` for every condition the original script
    `throw`s on: unsupported Python version, the spawned process exiting
    before it answers, no response within the startup window, or a
    response that never reports healthy (outside the watcher's "started
    but not yet paired" case, which is not an error).
    """
    data_dir = data_dir if data_dir is not None else data_dir_for(root)
    check_python_version()

    runtime = get_live_role(data_dir, role, http_get_json=http_get_json)
    if runtime is None:
        data_dir.mkdir(parents=True, exist_ok=True)
        executable = python_executable or sys.executable
        process = spawn(
            [executable, "-m", "pymonitor.cli", role.cli_entry],
            cwd=str(root),
            stdout_path=data_dir / f"{role.name}.log",
            stderr_path=data_dir / f"{role.name}-error.log",
        )
        for _ in range(startup_attempts):
            sleep(startup_interval)
            runtime = get_live_role(data_dir, role, http_get_json=http_get_json)
            if runtime is not None:
                break
            if process.poll() is not None:
                raise LauncherError(
                    f"{role.name} exited before responding. See .local/{role.name}-error.log."
                )
        if runtime is None:
            raise LauncherError(f"{role.name} did not respond. See .local/{role.name}-error.log.")

    status: dict[str, Any] = {}
    for _ in range(health_attempts):
        status = http_get_json(f"{runtime['url']}{role.status_endpoint}", health_timeout)
        if status.get("healthy"):
            break
        if role.name == "watcher" and status.get("paired") is False:
            break
        sleep(startup_interval)

    if role.name == "watcher" and status.get("paired") is False:
        return StartResult(runtime=runtime, status=status, paired_pending=True)
    if not status.get("healthy"):
        raise LauncherError(
            f"{role.name} is running but not healthy. "
            f"Check .local/{role.name}-error.log and collector source coverage."
        )

    if role.name == "collector" and not no_browser and open_browser is not None:
        open_browser(runtime["url"])

    return StartResult(runtime=runtime, status=status, paired_pending=False)


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
