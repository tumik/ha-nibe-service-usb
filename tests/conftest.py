"""Shared fixtures."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nibe_service_usb.const import CONF_KEY, CONF_SERVER_ID, DOMAIN

from .fake_server import SERVER_ID, TEST_KEY, FakeServer


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Allow the harness to load custom_components/."""


@pytest.fixture(autouse=True)
def allow_sockets(socket_enabled: None) -> None:
    """The fake gateway listens on and is reached via 127.0.0.1 (pytest-socket would block it)."""


async def _no_mdns(hass: object, server_id: str, timeout: float = 4.0) -> None:
    return None


@pytest.fixture(autouse=True)
def no_real_relocation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never query the real mDNS from tests; test_relocate.py patches it itself."""
    monkeypatch.setattr("custom_components.nibe_service_usb.gateway.find_by_mdns", _no_mdns)


@pytest.fixture
async def fake_server() -> AsyncGenerator[FakeServer]:
    async with FakeServer() as server:
        yield server


@pytest.fixture
def mock_config_entry(fake_server: FakeServer) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=SERVER_ID,
        title="Nibe test",
        data={
            "host": "127.0.0.1",
            "port": fake_server.port,
            CONF_KEY: TEST_KEY.hex(),
            CONF_SERVER_ID: SERVER_ID,
        },
    )
