"""Smoke tests for scripts/verify_ui.py -- the Python port of the synthetic
dashboard UI fixture/browser-check harness (scripts/verify-ui.mjs).

This is dev-only tooling (scripts/check-ui.mjs drives it via headless Edge
for real UI assertions), so coverage here is intentionally shallow: confirm
the aiohttp app builds and serves its routes without crashing, that the
fixture data shape matches what the embedded measure.js expects (24 cards),
and that the dismiss-auth gate (the exact spot a prior redaction-paste bug
broke) rejects a missing/invalid token and accepts the real one.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "scripts" / "verify_ui.py"


def _load_verify_ui():
    """Import scripts/verify_ui.py as a module (it has no package __init__,
    and its PEP 723 header is not valid for a plain `import`)."""
    spec = importlib.util.spec_from_file_location("verify_ui", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("verify_ui", module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def verify_ui():
    return _load_verify_ui()


@pytest.fixture
async def client(verify_ui):
    app = verify_ui._build_app()
    async with TestClient(TestServer(app)) as test_client:
        yield test_client


async def test_fixture_builds_24_parents(verify_ui):
    parents = verify_ui._build_parents()
    assert len(parents) == 24


async def test_root_serves_host_shell(client):
    response = await client.get("/")
    assert response.status == 200
    text = await response.text()
    assert '<script src="/host.js">' in text


async def test_case_serves_index_html_with_measure_script_injected(client):
    response = await client.get("/case")
    assert response.status == 200
    text = await response.text()
    assert '<script src="/measure.js" defer></script>' in text


async def test_static_assets_served(client):
    for path in ("/style.css", "/app.js"):
        response = await client.get(path)
        assert response.status == 200


async def test_measure_js_embeds_expected_fixture(client):
    response = await client.get("/measure.js")
    assert response.status == 200
    text = await response.text()
    assert "window.expected=" in text


async def test_api_status_without_fixture_id_rejected(client):
    response = await client.get("/api/status")
    # No Referer header -> no fixture id -> the error middleware maps the
    # RuntimeError("Fixture ID required") to a 500, same as the .mjs original.
    assert response.status == 500


async def test_api_status_returns_24_sessions(client):
    response = await client.get(
        "/api/status", headers={"Referer": "http://example.invalid/case?fixture=abc&mode=light"}
    )
    assert response.status == 200
    payload = await response.json()
    assert len(payload["sessions"]) == 24
    assert payload["theme"] == {"mode": "light", "source": "windows-apps"}


async def test_api_dismiss_requires_valid_control_token(client):
    response = await client.post(
        "/api/dismiss",
        headers={"Referer": "http://example.invalid/case?fixture=abc"},
        data=json.dumps({"entries": []}),
    )
    assert response.status == 403

    response = await client.post(
        "/api/dismiss",
        headers={
            "Referer": "http://example.invalid/case?fixture=abc",
            "Authorization": "Bearer not-the-real-token",
        },
        data=json.dumps({"entries": []}),
    )
    assert response.status == 403


async def test_api_dismiss_accepts_real_control_token(client):
    referer = "http://example.invalid/case?fixture=xyz"
    control = await client.get("/api/control", headers={"Referer": referer})
    assert control.status == 200
    token = (await control.json())["token"]

    status = await client.get("/api/status", headers={"Referer": referer})
    assert status.status == 200
    sessions = (await status.json())["sessions"]
    dismissable = next(row for row in sessions if row.get("dismissKey"))
    entry = {"id": dismissable["id"], "key": dismissable["dismissKey"]}

    auth_value = "Bearer " + token
    response = await client.post(
        "/api/dismiss",
        headers={
            "Referer": referer,
            "Authorization": auth_value,
        },
        data=json.dumps({"entries": [entry]}),
    )
    assert response.status == 200
    payload = await response.json()
    assert dismissable["id"] in payload["dismissed"]
