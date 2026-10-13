"""Shared pytest fixtures.

Most tests exercise LocalSource's *merge* logic (how a CLI-only session gets
folded into samples/hierarchy), not the SDK discovery mechanism itself. To
keep that decoupled from a real github-copilot-sdk install / subprocess
spawn, every test transparently gets a directory-scan stand-in for
`LocalSource._default_sdk_discover`. It deliberately returns the supported
legacy string-id shape; focused source tests supply richer SDK metadata.
"""
from __future__ import annotations

import os

import pytest

from pymonitor.source import SESSION_ID, LocalSource


@pytest.fixture(autouse=True)
def directory_scan_sdk_discover(monkeypatch: pytest.MonkeyPatch):
    production_discover = LocalSource._default_sdk_discover

    async def _scan(self: LocalSource) -> list[str]:
        try:
            with os.scandir(self.root) as it:
                return [entry.name for entry in it if entry.is_dir() and SESSION_ID.match(entry.name)]
        except FileNotFoundError:
            return []

    monkeypatch.setattr(LocalSource, "_default_sdk_discover", _scan)
    return production_discover
