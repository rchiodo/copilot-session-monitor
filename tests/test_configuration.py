"""Tests for pymonitor.configuration -- the literal port of configuration.mjs.

No dedicated configuration.test.mjs existed in the Node app, so this suite is
newly authored (not ported) but exercises the exact same surface described by
configuration.mjs: config read/save round-trips, collector-config validation,
first-run `initialize()` guards, `pair()` local/remote flows and conflict
guards, `ensure_local_reporter`, `pair_connection_string`, `migrate_legacy`,
and the CLI dispatcher (`configuration_command`).

Every test monkeypatches `configuration.data_dir` to an isolated `tmp_path`
and stubs `_harden_acl` (a no-op outside Windows) so the suite is fast and
platform-independent; the real `_harden_acl`/`_generate_certificate` native
calls were manually smoke-tested against a real temp directory (ACL
inspection via win32security, and `ssl.SSLContext.load_cert_chain`) before
writing this suite -- see docs/porting-notes.md.
"""
from __future__ import annotations

import datetime
import ipaddress
import json
import ssl
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from pymonitor import configuration as cfg
from pymonitor import protocol as proto

LOCAL_IP = "192.168.1.50"

# Captured before any test monkeypatches cfg._harden_acl to a no-op, so the
# dedicated ACL test can restore and exercise the real implementation.
_REAL_HARDEN_ACL = cfg._harden_acl


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data_dir = tmp_path / ".local"
    monkeypatch.setattr(cfg, "data_dir", data_dir)
    # _harden_acl is exercised directly by test_harden_acl_sets_owner_and_dacl
    # (Windows-only); stub it elsewhere so the rest of the suite stays fast
    # and platform-independent.
    monkeypatch.setattr(cfg, "_harden_acl", lambda path: None)
    monkeypatch.setattr(cfg, "_local_interface_addresses", lambda: [LOCAL_IP, "127.0.0.1"])
    return data_dir


def _base_collector(**overrides: Any) -> dict[str, Any]:
    config = {
        "version": 1,
        "id": "00000000-0000-0000-0000-000000000001",
        "bindAddress": LOCAL_IP,
        "port": 43188,
        "reporters": [],
    }
    config.update(overrides)
    return config


# --------------------------------------------------------------------------
# Config file round-trip
# --------------------------------------------------------------------------


async def test_save_and_read_config_round_trip() -> None:
    await cfg.save_config("thing.json", {"a": 1})
    assert await cfg.read_config("thing.json") == {"a": 1}


async def test_optional_config_missing_file_returns_none() -> None:
    assert await cfg.optional_config("missing.json") is None


async def test_optional_config_returns_value_when_present() -> None:
    await cfg.save_config("present.json", [1, 2, 3])
    assert await cfg.optional_config("present.json") == [1, 2, 3]


async def test_save_config_writes_atomically_no_tmp_file_left_behind(tmp_path: Path) -> None:
    await cfg.save_config("atomic.json", {"x": True})
    data_dir = cfg.data_dir
    assert (data_dir / "atomic.json").exists()
    assert not (data_dir / "atomic.json.tmp").exists()


# --------------------------------------------------------------------------
# validate_collector
# --------------------------------------------------------------------------


def test_validate_collector_accepts_well_formed_config() -> None:
    config = _base_collector()
    assert cfg.validate_collector(config) is config


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": 2},
        {"id": "not-a-uuid"},
        {"bindAddress": "8.8.8.8"},  # not a private address
        {"port": 80},
        {"port": 70000},
        {"port": 1024.5},
        {"reporters": "nope"},
    ],
)
def test_validate_collector_rejects_bad_top_level_fields(overrides: dict[str, Any]) -> None:
    config = _base_collector(**overrides)
    with pytest.raises(ValueError):
        cfg.validate_collector(config)


def test_validate_collector_rejects_bad_reporter_rows() -> None:
    bad_reporter = {
        "id": "00000000-0000-0000-0000-000000000002",
        "label": "",
        "tokenHash": "a" * 64,
        "legacy": False,
    }
    config = _base_collector(reporters=[bad_reporter])
    with pytest.raises(ValueError):
        cfg.validate_collector(config)


def test_validate_collector_rejects_duplicate_reporter_ids() -> None:
    row = {
        "id": "00000000-0000-0000-0000-000000000002",
        "label": "dup",
        "tokenHash": "a" * 64,
        "legacy": False,
    }
    config = _base_collector(reporters=[row, dict(row)])
    with pytest.raises(ValueError):
        cfg.validate_collector(config)


def test_validate_collector_rejects_more_than_one_legacy_row() -> None:
    def row(n: int, *, legacy: bool) -> dict[str, Any]:
        return {
            "id": f"00000000-0000-0000-0000-00000000000{n}",
            "label": f"row{n}",
            "tokenHash": "a" * 64,
            "legacy": legacy,
        }

    config = _base_collector(reporters=[row(2, legacy=True), row(3, legacy=True)])
    with pytest.raises(ValueError):
        cfg.validate_collector(config)


# --------------------------------------------------------------------------
# detect_lan_address()
# --------------------------------------------------------------------------


def test_detect_lan_address_prefers_default_route_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cfg, "_local_interface_addresses", lambda: ["127.0.0.1", "169.254.1.2", LOCAL_IP, "10.0.0.9"]
    )
    monkeypatch.setattr(cfg, "_default_route_probe_address", lambda: "10.0.0.9")
    assert cfg.detect_lan_address() == "10.0.0.9"


def test_detect_lan_address_falls_back_when_probe_address_not_a_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Probe returns an address psutil doesn't see as a local interface (or a
    # loopback/link-local one) -- fall back to the first real candidate
    # instead of trusting it blindly.
    monkeypatch.setattr(cfg, "_local_interface_addresses", lambda: ["127.0.0.1", LOCAL_IP])
    monkeypatch.setattr(cfg, "_default_route_probe_address", lambda: "203.0.113.5")
    assert cfg.detect_lan_address() == LOCAL_IP


def test_detect_lan_address_falls_back_when_probe_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "_local_interface_addresses", lambda: ["127.0.0.1", LOCAL_IP])
    monkeypatch.setattr(cfg, "_default_route_probe_address", lambda: None)
    assert cfg.detect_lan_address() == LOCAL_IP


def test_detect_lan_address_excludes_loopback_and_link_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cfg, "_local_interface_addresses", lambda: ["127.0.0.1", "169.254.3.4", LOCAL_IP]
    )
    monkeypatch.setattr(cfg, "_default_route_probe_address", lambda: None)
    assert cfg.detect_lan_address() == LOCAL_IP


def test_detect_lan_address_raises_when_no_private_address_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cfg, "_local_interface_addresses", lambda: ["127.0.0.1", "169.254.3.4"])
    monkeypatch.setattr(cfg, "_default_route_probe_address", lambda: None)
    with pytest.raises(RuntimeError, match="No private LAN IPv4 address"):
        cfg.detect_lan_address()


# --------------------------------------------------------------------------
# initialize()
# --------------------------------------------------------------------------


async def test_initialize_first_run_creates_config_and_certificate() -> None:
    config = await cfg.initialize(LOCAL_IP, 43188, False)
    assert config["bindAddress"] == LOCAL_IP
    assert config["port"] == 43188
    assert config["reporters"] == []
    assert (cfg.data_dir / "collector.json").exists()
    assert (cfg.data_dir / "collector-cert.pem").exists()
    assert (cfg.data_dir / "collector-key.pem").exists()

    # The generated PEM pair must be directly consumable by ssl.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(
        certfile=str(cfg.data_dir / "collector-cert.pem"),
        keyfile=str(cfg.data_dir / "collector-key.pem"),
    )


async def test_initialize_second_call_without_reconfigure_short_circuits() -> None:
    first = await cfg.initialize(LOCAL_IP, 43188, False)
    cert_mtime_before = (cfg.data_dir / "collector-cert.pem").stat().st_mtime_ns
    second = await cfg.initialize("10.0.0.5", 9999, False)
    assert second == first
    # Cert must not be regenerated on the short-circuit path.
    assert (cfg.data_dir / "collector-cert.pem").stat().st_mtime_ns == cert_mtime_before


async def test_initialize_reconfigure_true_regenerates_with_new_bind_address() -> None:
    await cfg.initialize(LOCAL_IP, 43188, False)
    second = await cfg.initialize(LOCAL_IP, 43189, True)
    assert second["port"] == 43189


async def test_initialize_rejects_bind_address_not_in_local_interfaces() -> None:
    with pytest.raises(RuntimeError, match="assigned local/private IP"):
        await cfg.initialize("192.168.99.99", 43188, False)


async def test_initialize_rejects_public_bind_address() -> None:
    # 8.8.8.8 is not private even if (hypothetically) present locally.
    with pytest.raises(RuntimeError, match="assigned local/private IP"):
        await cfg.initialize("8.8.8.8", 43188, False)


async def test_initialize_refuses_when_runtime_json_present() -> None:
    await cfg.save_config("runtime.json", {"pid": 1234})
    with pytest.raises(RuntimeError, match="Stop the collector"):
        await cfg.initialize(LOCAL_IP, 43188, False)


async def test_initialize_refreshes_local_watcher_certificate_on_reconfigure() -> None:
    await cfg.initialize(LOCAL_IP, 43188, False)
    config = await cfg.load_collector()
    reporter_id = await cfg.ensure_local_reporter(config)
    await cfg.save_config(
        "watcher.json",
        {
            "version": 1,
            "reporterId": reporter_id,
            "label": "local",
            "collectorUrl": "https://127.0.0.1:43188",
            "token": "a" * 64,
            "certificate": "stale",
        },
    )
    await cfg.initialize(LOCAL_IP, 43190, True)
    watcher = await cfg.read_config("watcher.json")
    assert watcher["collectorUrl"] == "https://127.0.0.1:43190"
    assert watcher["certificate"] != "stale"


async def test_initialize_refreshes_remote_pairing_certificate_when_token_matches() -> None:
    await cfg.initialize(LOCAL_IP, 43188, False)
    pairing_path = await cfg.pair("remote-watcher")
    pairing_file = Path(pairing_path).name
    await cfg.initialize(LOCAL_IP, 43191, True)
    pairing = await cfg.read_config(pairing_file)
    assert pairing["collectorUrl"] == f"https://{LOCAL_IP}:43191"


# --------------------------------------------------------------------------
# initialize() -- legacy Node/PFX key-recovery migration gap
# --------------------------------------------------------------------------


def _build_legacy_cert_and_key() -> tuple[bytes, bytes, bytes]:
    """Build a self-signed cert+key pair and export it the way certificate.ps1 did:
    cert-only PEM (collector-cert.pem) plus a PFX bundling cert+key with an empty
    password (collector.pfx) -- but, matching the old Node app, no separate PEM key.

    Returns (cert_pem, pfx_bytes, public_key_der) for assertions.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Local Copilot Monitor")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365 * 2))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    pfx_bytes = pkcs12.serialize_key_and_certificates(
        name=b"collector",
        key=key,
        cert=cert,
        cas=None,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_key_der = key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return cert_pem, pfx_bytes, public_key_der


async def _write_legacy_collector_json() -> None:
    await cfg.save_config("collector.json", _base_collector())


async def test_initialize_recovers_key_from_legacy_pfx_preserving_cert() -> None:
    cert_pem, pfx_bytes, public_key_der = _build_legacy_cert_and_key()
    await _write_legacy_collector_json()
    cert_path = cfg.data_dir / "collector-cert.pem"
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(cert_pem)
    (cfg.data_dir / "collector.pfx").write_bytes(pfx_bytes)

    await cfg.initialize(LOCAL_IP, 43188, False)

    key_path = cfg.data_dir / "collector-key.pem"
    assert key_path.exists()
    # Cert must be byte-for-byte unchanged -- fingerprint preserved, no re-pairing needed.
    assert cert_path.read_bytes() == cert_pem

    recovered_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    recovered_public_der = recovered_key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    assert recovered_public_der == public_key_der

    # The recovered pair must actually be usable by ssl.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))


async def test_initialize_falls_back_to_fresh_pair_when_no_legacy_pfx_available() -> None:
    cert_pem, _pfx_bytes, _public_key_der = _build_legacy_cert_and_key()
    await _write_legacy_collector_json()
    cert_path = cfg.data_dir / "collector-cert.pem"
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(cert_pem)
    # Deliberately no collector.pfx on disk.

    await cfg.initialize(LOCAL_IP, 43188, False)

    key_path = cfg.data_dir / "collector-key.pem"
    assert cert_path.exists()
    assert key_path.exists()
    # A fresh pair is fine here -- there was nothing recoverable to preserve.
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))


async def test_initialize_is_idempotent_when_both_pem_files_already_exist() -> None:
    await cfg.initialize(LOCAL_IP, 43188, False)
    cert_path = cfg.data_dir / "collector-cert.pem"
    key_path = cfg.data_dir / "collector-key.pem"
    cert_bytes_before = cert_path.read_bytes()
    key_bytes_before = key_path.read_bytes()

    await cfg.initialize(LOCAL_IP, 43188, False)
    await cfg.initialize(LOCAL_IP, 43188, False)

    assert cert_path.read_bytes() == cert_bytes_before
    assert key_path.read_bytes() == key_bytes_before


async def test_initialize_fresh_install_unaffected_by_pfx_recovery_path() -> None:
    # No collector.json at all -- the ordinary first-run path, untouched by this fix.
    config = await cfg.initialize(LOCAL_IP, 43188, False)
    assert config["bindAddress"] == LOCAL_IP
    assert (cfg.data_dir / "collector-cert.pem").exists()
    assert (cfg.data_dir / "collector-key.pem").exists()
    assert not (cfg.data_dir / "collector.pfx").exists()


async def test_initialize_reconfigure_true_still_fully_regenerates_despite_recoverable_pfx() -> None:
    cert_pem, pfx_bytes, _public_key_der = _build_legacy_cert_and_key()
    await _write_legacy_collector_json()
    cert_path = cfg.data_dir / "collector-cert.pem"
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(cert_pem)
    (cfg.data_dir / "collector.pfx").write_bytes(pfx_bytes)

    await cfg.initialize(LOCAL_IP, 43188, True)

    # reconfigure=True must still produce a brand-new cert (different fingerprint),
    # not the recovered/preserved legacy one.
    assert cert_path.read_bytes() != cert_pem


# --------------------------------------------------------------------------
# pair()
# --------------------------------------------------------------------------


async def test_pair_local_creates_watcher_json() -> None:
    await cfg.initialize(LOCAL_IP, 43188, False)
    result = await cfg.pair("this-machine", True)
    assert Path(result).name == "watcher.json"
    watcher = await cfg.read_config("watcher.json")
    assert watcher["label"] == "this-machine"
    assert watcher["collectorUrl"] == f"https://127.0.0.1:43188"
    config = await cfg.load_collector()
    assert any(row["legacy"] for row in config["reporters"])


async def test_pair_local_second_call_returns_existing_watcher_path() -> None:
    await cfg.initialize(LOCAL_IP, 43188, False)
    await cfg.pair("this-machine", True)
    result = await cfg.pair("this-machine", True)
    assert Path(result).name == "watcher.json"


async def test_pair_local_conflicts_with_existing_foreign_watcher_identity() -> None:
    await cfg.initialize(LOCAL_IP, 43188, False)
    # A watcher.json that refers to a reporterId not present/legacy in collector.json
    # simulates "this watcher is paired elsewhere".
    await cfg.save_config(
        "watcher.json",
        {
            "version": 1,
            "reporterId": "99999999-9999-9999-9999-999999999999",
            "label": "elsewhere",
            "collectorUrl": "https://127.0.0.1:1",
            "token": "a" * 64,
            "certificate": "x",
        },
    )
    with pytest.raises(RuntimeError, match="paired elsewhere"):
        await cfg.pair("this-machine", True)


async def test_pair_local_conflicts_when_legacy_row_exists_but_watcher_json_missing() -> None:
    await cfg.initialize(LOCAL_IP, 43188, False)
    config = await cfg.load_collector()
    await cfg.ensure_local_reporter(config)
    with pytest.raises(RuntimeError, match="watcher profile is missing"):
        await cfg.pair("this-machine", True)


async def test_pair_remote_creates_pairing_file_and_appends_reporter() -> None:
    await cfg.initialize(LOCAL_IP, 43188, False)
    result = await cfg.pair("laptop")
    assert Path(result).name.startswith("pairing-")
    pairing = await cfg.read_config(Path(result).name)
    assert pairing["label"] == "laptop"
    assert pairing["collectorUrl"] == f"https://{LOCAL_IP}:43188"
    config = await cfg.load_collector()
    assert len(config["reporters"]) == 1
    assert config["reporters"][0]["legacy"] is False


async def test_pair_connection_string_round_trips_through_encode_decode() -> None:
    await cfg.initialize(LOCAL_IP, 43188, False)
    encoded = await cfg.pair_connection_string("laptop")
    decoded = proto.decode_connection_string(encoded)
    assert decoded["label"] == "laptop"
    assert decoded["collectorUrl"] == f"https://{LOCAL_IP}:43188"


# --------------------------------------------------------------------------
# ensure_local_reporter()
# --------------------------------------------------------------------------


async def test_ensure_local_reporter_creates_row_when_absent() -> None:
    await cfg.initialize(LOCAL_IP, 43188, False)
    config = await cfg.load_collector()
    reporter_id = await cfg.ensure_local_reporter(config)
    saved = await cfg.load_collector()
    assert any(row["id"] == reporter_id and row["legacy"] for row in saved["reporters"])


async def test_ensure_local_reporter_is_idempotent() -> None:
    await cfg.initialize(LOCAL_IP, 43188, False)
    config = await cfg.load_collector()
    first = await cfg.ensure_local_reporter(config)
    second = await cfg.ensure_local_reporter(config)
    assert first == second
    assert sum(1 for row in config["reporters"] if row["legacy"]) == 1


# --------------------------------------------------------------------------
# migrate_legacy()
# --------------------------------------------------------------------------


async def test_migrate_legacy_noop_when_collector_file_already_exists(tmp_path: Path) -> None:
    collector_file = tmp_path / "collector.json"
    collector_file.write_text("{}")
    # Should return immediately without requiring sessions.json/local reporter.
    await cfg.migrate_legacy({"reporters": []}, str(collector_file))


async def test_migrate_legacy_noop_when_no_legacy_sessions_file(tmp_path: Path) -> None:
    collector_file = tmp_path / "collector-missing.json"
    await cfg.migrate_legacy({"reporters": []}, str(collector_file))
    assert not any(cfg.data_dir.glob("backup-*"))


async def test_migrate_legacy_requires_local_reporter_identity(tmp_path: Path) -> None:
    collector_file = tmp_path / "collector-missing.json"
    await cfg.save_config("sessions.json", {"sessions": []})
    with pytest.raises(RuntimeError, match="local reporter identity"):
        await cfg.migrate_legacy({"reporters": []}, str(collector_file))


async def test_migrate_legacy_backs_up_and_seeds_watcher_sessions(tmp_path: Path) -> None:
    collector_file = tmp_path / "collector-missing.json"
    await cfg.save_config("sessions.json", {"sessions": ["legacy"]})
    await cfg.save_config("notifications.json", {"notifications": []})
    local_row = {
        "id": "00000000-0000-0000-0000-0000000000aa",
        "label": "host",
        "tokenHash": "a" * 64,
        "legacy": True,
    }
    await cfg.migrate_legacy({"reporters": [local_row]}, str(collector_file))
    backups = list(cfg.data_dir.glob("backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "sessions.json").exists()
    assert (backups[0] / "notifications.json").exists()
    watcher_sessions = await cfg.read_config("watcher-sessions.json")
    assert watcher_sessions == {"sessions": ["legacy"]}


async def test_migrate_legacy_does_not_overwrite_existing_watcher_sessions(tmp_path: Path) -> None:
    collector_file = tmp_path / "collector-missing.json"
    await cfg.save_config("sessions.json", {"sessions": ["legacy"]})
    await cfg.save_config("watcher-sessions.json", {"sessions": ["already-migrated"]})
    local_row = {
        "id": "00000000-0000-0000-0000-0000000000aa",
        "label": "host",
        "tokenHash": "a" * 64,
        "legacy": True,
    }
    await cfg.migrate_legacy({"reporters": [local_row]}, str(collector_file))
    watcher_sessions = await cfg.read_config("watcher-sessions.json")
    assert watcher_sessions == {"sessions": ["already-migrated"]}


# --------------------------------------------------------------------------
# configuration_command() CLI dispatcher
# --------------------------------------------------------------------------


async def test_configuration_command_initialize(capsys: pytest.CaptureFixture[str]) -> None:
    await cfg.configuration_command(["initialize", LOCAL_IP, "43188"])
    out = capsys.readouterr().out
    assert "Collector configured" in out
    assert (cfg.data_dir / "collector.json").exists()


async def test_configuration_command_local(capsys: pytest.CaptureFixture[str]) -> None:
    await cfg.configuration_command(["local"])
    out = capsys.readouterr().out
    assert "Local watcher pairing ready" in out
    assert (cfg.data_dir / "watcher.json").exists()


async def test_configuration_command_pair_prints_fingerprint(capsys: pytest.CaptureFixture[str]) -> None:
    await cfg.configuration_command(["initialize", LOCAL_IP, "43188"])
    await cfg.configuration_command(["pair", "laptop"])
    out = capsys.readouterr().out
    assert "Private pairing file:" in out
    assert "Certificate SHA256:" in out
    fingerprint_line = next(line for line in out.splitlines() if "Certificate SHA256" in line)
    fingerprint = fingerprint_line.split(":", 1)[1].strip()
    assert all(part and len(part) == 2 for part in fingerprint.split(":"))


async def test_configuration_command_import_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    await cfg.configuration_command(["initialize", LOCAL_IP, "43188"])
    pairing_path = await cfg.pair("remote-watcher")
    export_path = tmp_path / "exported-pairing.json"
    export_path.write_text(Path(pairing_path).read_text(encoding="utf-8"), encoding="utf-8")

    # Reset data_dir to simulate a separate watcher installation importing the file.
    other_data_dir = tmp_path / "other-install"
    cfg.data_dir = other_data_dir
    await cfg.configuration_command(["import", str(export_path)])
    out = capsys.readouterr().out
    assert "Imported pairing for remote-watcher" in out
    assert (other_data_dir / "watcher.json").exists()


async def test_configuration_command_revoke(capsys: pytest.CaptureFixture[str]) -> None:
    await cfg.configuration_command(["initialize", LOCAL_IP, "43188"])
    pairing_path = await cfg.pair("laptop")
    reporter_id = Path(pairing_path).name.removeprefix("pairing-").removesuffix(".json")
    await cfg.configuration_command(["revoke", reporter_id])
    out = capsys.readouterr().out
    assert "Reporter credential revoked" in out
    config = await cfg.load_collector()
    assert not any(row["id"] == reporter_id for row in config["reporters"])


async def test_configuration_command_revoke_unknown_reporter_raises() -> None:
    await cfg.configuration_command(["initialize", LOCAL_IP, "43188"])
    with pytest.raises(RuntimeError, match="Unknown remote reporter"):
        await cfg.configuration_command(["revoke", "99999999-9999-9999-9999-999999999999"])


async def test_configuration_command_unknown_subcommand_raises() -> None:
    with pytest.raises(RuntimeError, match="Expected initialize, local, pair, import, or revoke"):
        await cfg.configuration_command([])


# --------------------------------------------------------------------------
# Native cert generation / fingerprint (direct unit coverage, not via CLI)
# --------------------------------------------------------------------------


def test_generate_certificate_produces_ssl_loadable_pem_pair() -> None:
    cfg._generate_certificate(LOCAL_IP)
    cert_file = cfg.data_dir / "collector-cert.pem"
    key_file = cfg.data_dir / "collector-key.pem"
    assert cert_file.exists()
    assert key_file.exists()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=str(cert_file), keyfile=str(key_file))


def test_fingerprint_sha256_matches_node_x509_fingerprint256_format() -> None:
    cfg._generate_certificate(LOCAL_IP)
    cert_file = cfg.data_dir / "collector-cert.pem"
    fingerprint = cfg._fingerprint_sha256(cert_file.read_bytes())
    parts = fingerprint.split(":")
    assert len(parts) == 32  # SHA-256 == 32 bytes
    assert all(len(part) == 2 and part == part.upper() for part in parts)
    # Must also work when given the certificate as a PEM string encoded to bytes
    # (the `import`/`pair` CLI call sites both pass PEM text, never DER).
    pem_text = cert_file.read_text(encoding="utf-8")
    assert cfg._fingerprint_sha256(pem_text.encode("utf-8")) == fingerprint


# --------------------------------------------------------------------------
# _protect_data() broad-directory guard
# --------------------------------------------------------------------------


async def test_protect_data_refuses_home_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cfg, "data_dir", Path.home())
    with pytest.raises(RuntimeError, match="Refusing to use a broad directory"):
        await cfg._protect_data()


async def test_protect_data_allows_normal_subdirectory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / ".local"
    monkeypatch.setattr(cfg, "data_dir", target)
    await cfg._protect_data()
    assert target.exists()


# --------------------------------------------------------------------------
# _harden_acl() -- real native call, Windows only (unstubbed, unlike the rest
# of this suite). Verifies owner + a FullControl DACL for current user and
# SYSTEM, mirroring windows/protect-data.ps1's DirectorySecurity semantics.
# --------------------------------------------------------------------------


@pytest.mark.skipif(__import__("platform").system() != "Windows", reason="ACL hardening is Windows-only")
def test_harden_acl_sets_owner_and_dacl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import win32api
    import win32security

    # The autouse _isolated_data_dir fixture stubs cfg._harden_acl to a no-op for
    # every other test; restore the real implementation for this one.
    monkeypatch.setattr(cfg, "_harden_acl", _REAL_HARDEN_ACL)

    target = tmp_path / "acl-target"
    target.mkdir()
    cfg._harden_acl(target)

    sd = win32security.GetFileSecurity(
        str(target),
        win32security.OWNER_SECURITY_INFORMATION | win32security.DACL_SECURITY_INFORMATION,
    )
    owner_sid = sd.GetSecurityDescriptorOwner()
    owner_name, _, _ = win32security.LookupAccountSid(None, owner_sid)
    assert owner_name == win32api.GetUserName()

    dacl = sd.GetSecurityDescriptorDacl()
    assert dacl.GetAceCount() == 2  # current user + SYSTEM, nothing else
