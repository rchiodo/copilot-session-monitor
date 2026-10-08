"""Port of src/configuration.mjs.

Literal port of the collector/watcher configuration CLI: config file
read/write, collector-config validation, first-run initialization,
pairing (local self-watcher and remote), connection-string export, and
legacy single-machine state migration.

Two deviations from the Node original, both because the originals shelled
out to PowerShell scripts that exist only to do things Node's TLS stack
can't do natively, but Python's can:

- **Native PEM certificate generation** (`_generate_certificate`) replaces
  `windows/certificate.ps1`. Node's `https.createServer({ pfx })` only
  consumes PKCS12, so the PS1 script built a PFX via .NET. Python's `ssl`
  module has no PKCS12 support but consumes a PEM cert+key pair directly via
  `SSLContext.load_cert_chain(certfile, keyfile)`, so the Python port
  generates a PEM cert/key pair straight from the `cryptography` library and
  never needs a PFX at all. A new `collector-key.pem` file is written
  alongside `collector-cert.pem` (the Node version never wrote a separate
  key file because the PFX bundled both). `windows/certificate.ps1` is
  retained on disk for historical/Node-only reference but is not invoked by
  this module.
- **Native ACL hardening** (`_harden_acl`) replaces `windows/protect-data.ps1`.
  Same NTFS ACL semantics (owner = current user, `FullControl` to the
  current user and `SYSTEM`, protected/non-inheriting ACL), applied directly
  via `pywin32` (`win32security`) instead of shelling out to PowerShell.
  `windows/protect-data.ps1` is likewise retained but unused by this module.

See docs/porting-notes.md for the full Phase 2 decision record.
"""
from __future__ import annotations

import asyncio
import datetime
import ipaddress
import json
import os
import platform
import re
import secrets
import shutil
import socket
import tempfile
import uuid
from pathlib import Path
from typing import Any

import psutil
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .protocol import (
    HASH_RE,
    UUID_RE,
    digest,
    encode_connection_string,
    private_address,
    validate_pairing,
)

if platform.system() == "Windows":  # pragma: no cover - exercised only on Windows
    import ntsecuritycon as con
    import win32api
    import win32security

__all__ = [
    "root",
    "data_dir",
    "powershell",
    "read_config",
    "optional_config",
    "save_config",
    "validate_collector",
    "load_collector",
    "initialize",
    "pair",
    "ensure_local_reporter",
    "pair_connection_string",
    "migrate_legacy",
    "configuration_command",
]

# src/pymonitor/configuration.py -> src/pymonitor -> src -> project root.
root = Path(__file__).resolve().parent.parent.parent
data_dir = Path(os.environ.get("MONITOR_DATA_DIR") or (root / ".local")).resolve()
powershell = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"

_COLLECTOR_CERT_FILE = "collector-cert.pem"
_COLLECTOR_KEY_FILE = "collector-key.pem"


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _normcase(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


async def _protect_data() -> None:
    broad = (Path(data_dir.anchor), root, Path.home(), Path(tempfile.gettempdir()))
    if any(_normcase(value) == _normcase(data_dir) for value in broad):
        raise RuntimeError("Refusing to use a broad directory for private monitor data")
    data_dir.mkdir(parents=True, exist_ok=True)
    if platform.system() == "Windows":
        _harden_acl(data_dir)


def _harden_acl(path: Path) -> None:
    """Owner = current user; FullControl to current user + SYSTEM only; non-inheriting ACL.

    Native equivalent of windows/protect-data.ps1's DirectorySecurity logic.
    """
    user_sid, _, _ = win32security.LookupAccountName("", win32api.GetUserName())
    system_sid = win32security.ConvertStringSidToSid("S-1-5-18")
    dacl = win32security.ACL()
    for sid in (user_sid, system_sid):
        dacl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION_DS,
            con.CONTAINER_INHERIT_ACE | con.OBJECT_INHERIT_ACE,
            con.FILE_ALL_ACCESS,
            sid,
        )
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.OWNER_SECURITY_INFORMATION
        | win32security.DACL_SECURITY_INFORMATION
        | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        user_sid,
        None,
        dacl,
        None,
    )


def _generate_certificate(bind_address: str) -> None:
    """Self-signed server cert, PEM cert+key pair. Native replacement for certificate.ps1.

    Matches certificate.ps1's parameters: RSA 2048, SHA-256, SAN of 127.0.0.1
    plus bindAddress (when distinct), CA:false, serverAuth EKU, validity from
    now-1day to now+2years.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Local Copilot Monitor")])
    san_addresses = [ipaddress.ip_address("127.0.0.1")]
    if bind_address != "127.0.0.1":
        san_addresses.append(ipaddress.ip_address(bind_address))
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365 * 2))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(a) for a in san_addresses]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / _COLLECTOR_CERT_FILE).write_bytes(cert_pem)
    key_path = data_dir / _COLLECTOR_KEY_FILE
    key_path.write_bytes(key_pem)
    try:
        os.chmod(key_path, 0o600)
    except OSError:
        pass


_COLLECTOR_PFX_FILE = "collector.pfx"


def _extract_key_from_legacy_pfx() -> bytes | None:
    """Recover the private key from the legacy Node PFX bundle (collector.pfx), if present.

    The old Node implementation exported cert+key together as a PFX with an empty
    password (see certificate.ps1, deleted; git history at 0771d20~1). Extracting the
    key from it lets an existing collector-cert.pem (also written by the old app) keep
    its original fingerprint -- so already-paired remote reporters don't need to
    re-pair -- while still producing the separate PEM key file that aiohttp's
    SSLContext.load_cert_chain() requires.
    """
    pfx_path = data_dir / _COLLECTOR_PFX_FILE
    try:
        pfx_bytes = pfx_path.read_bytes()
    except OSError:
        return None
    try:
        key, _cert, _chain = pkcs12.load_key_and_certificates(pfx_bytes, password=b"")
    except Exception:  # noqa: BLE001 - any malformed/incompatible PFX must not crash startup
        return None
    if key is None:
        return None
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def _ensure_certificate(bind_address: str) -> None:
    """Make sure collector-cert.pem/collector-key.pem both exist. Repairs a key-only
    migration gap from old Node/PFX installs by recovering the key from collector.pfx
    (preserving the existing cert/fingerprint) before falling back to generating a
    brand-new self-signed pair.
    """
    cert_path = data_dir / _COLLECTOR_CERT_FILE
    key_path = data_dir / _COLLECTOR_KEY_FILE
    if cert_path.exists() and key_path.exists():
        return
    if cert_path.exists() and not key_path.exists():
        recovered_key_pem = _extract_key_from_legacy_pfx()
        if recovered_key_pem is not None:
            data_dir.mkdir(parents=True, exist_ok=True)
            key_path.write_bytes(recovered_key_pem)
            try:
                os.chmod(key_path, 0o600)
            except OSError:
                pass
            return
    _generate_certificate(bind_address)


def _fingerprint_sha256(pem_or_der: bytes) -> str:
    """Colon-separated uppercase SHA-256 fingerprint, matching Node's X509Certificate.fingerprint256."""
    if pem_or_der.strip().startswith(b"-----BEGIN"):
        cert = x509.load_pem_x509_certificate(pem_or_der)
    else:
        cert = x509.load_der_x509_certificate(pem_or_der)
    raw = cert.fingerprint(hashes.SHA256())
    return ":".join(f"{byte:02X}" for byte in raw)


async def read_config(file: str) -> Any:
    return json.loads((data_dir / file).read_text(encoding="utf-8"))


async def optional_config(file: str) -> Any | None:
    try:
        return await read_config(file)
    except FileNotFoundError:
        return None


async def save_config(file: str, value: Any) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / file
    tmp = Path(f"{target}.tmp")
    tmp.write_text(json.dumps(value), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(target)


def validate_collector(config: dict[str, Any]) -> dict[str, Any]:
    reporters = config.get("reporters")

    def _bad_reporter(row: Any) -> bool:
        if not isinstance(row, dict):
            return True
        label = row.get("label")
        return (
            not (isinstance(row.get("id"), str) and UUID_RE.match(row["id"]))
            or not isinstance(label, str)
            or not label
            or len(label) > 80
            or bool(re.search(r"[\x00-\x1f]", label))
            or not (isinstance(row.get("tokenHash"), str) and HASH_RE.match(row["tokenHash"]))
            or not isinstance(row.get("legacy"), bool)
        )

    if (
        config.get("version") != 1
        or not (isinstance(config.get("id"), str) and UUID_RE.match(config["id"]))
        or not private_address(config.get("bindAddress"))
        or not _is_int(config.get("port"))
        or config["port"] < 1024
        or config["port"] > 65535
        or not isinstance(reporters, list)
        or len(reporters) > 100
        or any(_bad_reporter(row) for row in reporters)
        or len({row["id"] for row in reporters}) != len(reporters)
        or sum(1 for row in reporters if row["legacy"]) > 1
    ):
        raise ValueError("Invalid collector configuration")
    return config


async def load_collector() -> dict[str, Any]:
    return validate_collector(await read_config("collector.json"))


def _local_interface_addresses() -> list[str]:
    addresses: list[str] = []
    for entries in psutil.net_if_addrs().values():
        for entry in entries:
            if entry.address:
                addresses.append(entry.address.split("%", 1)[0])
    return addresses


async def initialize(
    bind_address: str = "127.0.0.1",
    port: int = 43188,
    reconfigure: bool = False,
) -> dict[str, Any]:
    existing = await optional_config("collector.json")
    if existing and not reconfigure:
        validated = validate_collector(existing)
        _ensure_certificate(validated["bindAddress"])
        return validated
    if await optional_config("runtime.json"):
        raise RuntimeError("Stop the collector before initializing or changing its listener")
    try:
        # Network-interface enumeration (psutil -> OS adapter APIs) is a
        # synchronous call with no built-in timeout. A VPN/virtual adapter
        # stuck in a transitional state can block it indefinitely, which
        # would otherwise hang this whole (single-threaded) process with no
        # port ever bound and no error -- a silent zombie. Run it off the
        # event loop thread and bound it so a stuck adapter fails loudly.
        addresses = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, _local_interface_addresses),
            timeout=10,
        )
    except TimeoutError as error:
        raise RuntimeError(
            "Timed out enumerating local network interfaces; a VPN or virtual "
            "adapter may be stuck. Check your network adapters and try again."
        ) from error
    if not private_address(bind_address) or bind_address not in addresses:
        raise RuntimeError("Select an assigned local/private IP (never an all-interface address)")
    config = validate_collector({
        "version": 1,
        "id": (existing or {}).get("id") or str(uuid.uuid4()),
        "bindAddress": bind_address,
        "port": port,
        "reporters": (existing or {}).get("reporters") or [],
    })
    await _protect_data()
    _generate_certificate(bind_address)
    await save_config("collector.json", config)
    local = await optional_config("watcher.json")
    if local and any(
        row["id"] == local.get("reporterId") and row["legacy"] for row in config["reporters"]
    ):
        local["certificate"] = (data_dir / _COLLECTOR_CERT_FILE).read_text(encoding="utf-8")
        local["collectorUrl"] = f"https://127.0.0.1:{port}"
        await save_config("watcher.json", local)
    for reporter in (row for row in config["reporters"] if not row["legacy"]):
        file = f"pairing-{reporter['id']}.json"
        bundle = await optional_config(file)
        if bundle and digest(bundle.get("token")) == reporter["tokenHash"]:
            bundle["certificate"] = (data_dir / _COLLECTOR_CERT_FILE).read_text(encoding="utf-8")
            host = bind_address
            bundle["collectorUrl"] = f"https://{f'[{host}]' if ':' in host else host}:{port}"
            await save_config(file, bundle)
    return config


async def pair(label: str, local: bool = False) -> str:
    config = await load_collector()
    if local and await optional_config("watcher.json"):
        existing = await read_config("watcher.json")
        if not any(
            row["id"] == existing.get("reporterId") and row["legacy"] for row in config["reporters"]
        ):
            raise RuntimeError(
                "This watcher is paired elsewhere; use collector-only mode or a separate installation directory"
            )
        return str(data_dir / "watcher.json")
    if local and any(row["legacy"] for row in config["reporters"]):
        raise RuntimeError("Local pairing exists but watcher profile is missing; restore it before continuing")
    await _protect_data()
    token = secrets.token_hex(32)
    reporter_id = str(uuid.uuid4())
    address = "127.0.0.1" if local else config["bindAddress"]
    pairing = validate_pairing({
        "version": 1,
        "reporterId": reporter_id,
        "label": label,
        "collectorUrl": f"https://{f'[{address}]' if ':' in address else address}:{config['port']}",
        "token": token,
        "certificate": (data_dir / _COLLECTOR_CERT_FILE).read_text(encoding="utf-8"),
    })
    config["reporters"].append({"id": reporter_id, "label": label, "tokenHash": digest(token), "legacy": local})
    validate_collector(config)
    file = "watcher.json" if local else f"pairing-{reporter_id}.json"
    await save_config(file, pairing)
    await save_config("collector.json", config)
    return str(data_dir / file)


async def ensure_local_reporter(config: dict[str, Any]) -> str:
    local = next((row for row in config["reporters"] if row["legacy"]), None)
    if not local:
        local = {
            "id": str(uuid.uuid4()),
            "label": socket.gethostname(),
            "tokenHash": digest(secrets.token_hex(32)),
            "legacy": True,
        }
        config["reporters"].append(local)
        validate_collector(config)
        await save_config("collector.json", config)
    return local["id"]


async def pair_connection_string(label: str) -> str:
    file = await pair(label)
    pairing = await read_config(Path(file).name)
    return encode_connection_string(pairing)


async def migrate_legacy(config: dict[str, Any], collector_file: str) -> None:
    if Path(collector_file).exists():
        return
    local = next((row for row in config["reporters"] if row["legacy"]), None)
    legacy = await optional_config("sessions.json")
    if not legacy:
        return
    if not local:
        raise RuntimeError(
            'Existing single-machine state requires a local reporter identity; run "python -m pymonitor.configuration local" '
            "once, or start the collector with self-observation enabled (the default)"
        )
    stamp = datetime.datetime.now(datetime.timezone.utc).isoformat().replace(":", "-").replace(".", "-")
    backup = data_dir / f"backup-{stamp}"
    backup.mkdir(parents=True)
    for file in ("sessions.json", "notifications.json"):
        source = data_dir / file
        try:
            shutil.copyfile(source, backup / file)
        except FileNotFoundError:
            pass
    if not await optional_config("watcher-sessions.json"):
        await save_config("watcher-sessions.json", legacy)
    print("Backed up legacy monitor metadata before migration.")


async def configuration_command(args: list[str]) -> None:
    command = args[0] if len(args) > 0 else None
    first = args[1] if len(args) > 1 else None
    second = args[2] if len(args) > 2 else None
    third = args[3] if len(args) > 3 else None

    if command == "initialize":
        await initialize(first or "127.0.0.1", int(second) if second is not None else 43188, third == "replace")
        print("Collector configured. No firewall or certificate-store changes were made.")
    elif command == "local":
        await initialize()
        await pair(socket.gethostname(), True)
        print("Local watcher pairing ready.")
    elif command == "pair":
        print(f"Private pairing file: {await pair(first)}")
        cert_pem = (data_dir / _COLLECTOR_CERT_FILE).read_bytes()
        print(f"Certificate SHA256: {_fingerprint_sha256(cert_pem)}")
        print("Transfer privately to the intended watcher. Do not share or commit this file.")
    elif command == "import":
        if await optional_config("watcher-runtime.json"):
            raise RuntimeError("Stop the watcher before importing a pairing")
        pairing = validate_pairing(json.loads(Path(first).resolve().read_text(encoding="utf-8")))
        previous = await optional_config("watcher.json")
        if previous and previous.get("reporterId") != pairing["reporterId"]:
            raise RuntimeError(
                "This checkout already has a different watcher identity; use a separate installation directory"
            )
        await _protect_data()
        await save_config("watcher.json", pairing)
        print(
            f"Imported pairing for {pairing['label']}. "
            f"Certificate SHA256: {_fingerprint_sha256(pairing['certificate'].encode('utf-8'))}"
        )
    elif command == "revoke":
        config = await load_collector()
        if not any(row["id"] == first and not row["legacy"] for row in config["reporters"]):
            raise RuntimeError("Unknown remote reporter")
        config["reporters"] = [row for row in config["reporters"] if row["id"] != first]
        await save_config("collector.json", config)
        print("Reporter credential revoked; retained metadata is preserved.")
    else:
        raise RuntimeError("Expected initialize, local, pair, import, or revoke")


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    import asyncio
    import sys

    try:
        asyncio.run(configuration_command(sys.argv[1:]))
    except Exception as error:  # noqa: BLE001 - mirrors the JS top-level catch
        print(f"Configuration failed: {error}", file=__import__("sys").stderr)
        raise SystemExit(1) from error
