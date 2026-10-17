"""Self-healing of the gateway address: discovery helpers, gateway relocation, flows."""

from __future__ import annotations

from collections.abc import AsyncGenerator, Generator
from ipaddress import ip_address
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_socket
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_ZEROCONF, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry
from zeroconf import IPVersion

from custom_components.nibe_service_usb import discovery
from custom_components.nibe_service_usb.const import (
    CONF_KEY,
    CONF_RELOCATE_MDNS,
    CONF_SERVER_ID,
    DOMAIN,
)

from .fake_server import SERVER_ID, TEST_KEY, FakeServer
from .test_init import setup_entry, state, wait_for

GW = "custom_components.nibe_service_usb.gateway"
OTHER_ID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def allow_loopback_aliases() -> Generator[None]:
    """The harness only allows 127.0.0.1; these tests also use 127.0.0.2 (nothing listens there)."""
    pytest_socket.socket_allow_hosts(["127.0.0.1", "127.0.0.2"])
    yield
    pytest_socket.socket_allow_hosts(["127.0.0.1"])


@pytest.fixture
async def other_server() -> AsyncGenerator[FakeServer]:
    async with FakeServer(server_id=OTHER_ID) as server:
        yield server


def old_address_entry(fake: FakeServer, key: str = TEST_KEY.hex(), **kwargs: Any) -> MockConfigEntry:
    """An entry pointing at 127.0.0.2, where nothing listens."""
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=SERVER_ID,
        title="Nibe test",
        data={"host": "127.0.0.2", "port": fake.port, CONF_KEY: key, CONF_SERVER_ID: SERVER_ID},
        **kwargs,
    )


def patch_relocation(
    monkeypatch: pytest.MonkeyPatch,
    found: tuple[str, int] | None,
    calls: list[str] | None = None,
) -> None:
    """Make the mDNS lookup return ``found`` (recording the call) and shorten the timers."""

    async def mdns(hass: HomeAssistant, server_id: str, timeout: float = 4.0) -> tuple[str, int] | None:
        assert server_id == SERVER_ID
        if calls is not None:
            calls.append("mdns")
        return found

    monkeypatch.setattr(f"{GW}.find_by_mdns", mdns)
    monkeypatch.setattr(f"{GW}.RELOCATE_AFTER", 0.1)
    monkeypatch.setattr(f"{GW}.RELOCATE_INTERVAL", 0.1)
    monkeypatch.setattr(f"{GW}.RECONNECT_MIN", 0.05)
    monkeypatch.setattr(f"{GW}.RECONNECT_MAX", 0.1)


# ------------------------------------------------------------------ pick_service


def service(server_id: str | None, addresses: list[str], port: int = 9477) -> SimpleNamespace:
    props = {b"proto": b"1"}
    if server_id is not None:
        props[b"id"] = server_id.encode()
    return SimpleNamespace(
        properties=props,
        port=port,
        parsed_addresses=lambda version=IPVersion.All: [a for a in addresses if ":" not in a],
    )


def test_pick_service() -> None:
    infos = [
        service(OTHER_ID, ["10.0.0.9"]),
        service(None, ["10.0.0.8"]),
        service(SERVER_ID, ["fe80::1", "192.168.1.50"], 9500),
    ]
    assert discovery.pick_service(infos, SERVER_ID) == ("192.168.1.50", 9500)
    assert discovery.pick_service(infos[:2], SERVER_ID) is None
    assert discovery.pick_service([service(SERVER_ID, [])], SERVER_ID) is None
    assert discovery.pick_service([], SERVER_ID) is None


async def test_find_by_mdns_without_zeroconf_returns_none(hass: HomeAssistant) -> None:
    assert await discovery.find_by_mdns(hass, SERVER_ID, timeout=0.1) is None


# ------------------------------------------------------------------ gateway relocation


async def test_relocates_after_failures(
    hass: HomeAssistant, fake_server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loaded at an address that dies, then relocated by the _run loop."""
    patch_relocation(monkeypatch, ("127.0.0.1", fake_server.port))
    entry = MockConfigEntry(
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
    await setup_entry(hass, entry)
    gateway = entry.runtime_data
    gateway.host = "127.0.0.2"  # reconnects now go to an address with nothing listening
    await fake_server.drop_connections()
    await wait_for(lambda: gateway.host == "127.0.0.1" and gateway.connected, limit=5)
    assert entry.data["host"] == "127.0.0.1"
    assert state(hass, "binary_sensor", "status_gateway_connected").state == "on"


async def test_startup_relocates_before_setup_retry(
    hass: HomeAssistant, fake_server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    patch_relocation(monkeypatch, ("127.0.0.1", fake_server.port), calls)
    monkeypatch.setattr(f"{GW}.RELOCATE_AFTER", 3600.0)  # the startup path ignores it
    entry = old_address_entry(fake_server)
    await setup_entry(hass, entry)
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data["host"] == "127.0.0.1"
    assert calls == ["mdns"]
    assert state(hass, "binary_sensor", "status_gateway_connected").state == "on"


async def test_mdns_disabled_by_option(
    hass: HomeAssistant, fake_server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    patch_relocation(monkeypatch, ("127.0.0.1", fake_server.port), calls)
    entry = old_address_entry(fake_server, options={CONF_RELOCATE_MDNS: False})
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert calls == []
    assert entry.data["host"] == "127.0.0.2"


async def test_not_found_stays_in_setup_retry(
    hass: HomeAssistant, fake_server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_relocation(monkeypatch, None)  # no mDNS announcement with the stored id
    entry = old_address_entry(fake_server)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.data["host"] == "127.0.0.2"


async def test_interval_limits_attempts(
    hass: HomeAssistant, fake_server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    patch_relocation(monkeypatch, None, calls)
    monkeypatch.setattr(f"{GW}.RELOCATE_INTERVAL", 3600.0)
    entry = old_address_entry(fake_server)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert len(calls) == 1
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert len(calls) == 1  # second setup attempt within the interval does not look again


async def test_zeroconf_updates_entry_in_setup_retry(hass: HomeAssistant, fake_server: FakeServer) -> None:
    entry = old_address_entry(fake_server)
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY

    info = ZeroconfServiceInfo(
        ip_address=ip_address("127.0.0.1"),
        ip_addresses=[ip_address("127.0.0.1")],
        hostname="nibe.local.",
        name="Garage._nibe-usb._tcp.local.",
        port=fake_server.port,
        properties={"id": SERVER_ID, "proto": "1"},
        type="_nibe-usb._tcp.local.",
    )
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_ZEROCONF}, data=info)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    await hass.async_block_till_done()
    assert entry.data["host"] == "127.0.0.1"
    assert entry.state is ConfigEntryState.LOADED


# ------------------------------------------------------------------ reconfigure


async def start_reconfigure(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )


async def test_reconfigure_success_keeps_key(hass: HomeAssistant, fake_server: FakeServer) -> None:
    entry = old_address_entry(fake_server)
    await setup_entry_unloaded(hass, entry)
    result = await start_reconfigure(hass, entry)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "127.0.0.1", "port": fake_server.port, CONF_KEY: ""}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    assert entry.data["host"] == "127.0.0.1"
    assert entry.data[CONF_KEY] == TEST_KEY.hex()
    assert entry.state is ConfigEntryState.LOADED


async def test_reconfigure_new_key(hass: HomeAssistant, fake_server: FakeServer) -> None:
    entry = old_address_entry(fake_server, key="ab" * 32)
    await setup_entry_unloaded(hass, entry)
    result = await start_reconfigure(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "127.0.0.1", "port": fake_server.port, CONF_KEY: TEST_KEY.hex()}
    )
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_KEY] == TEST_KEY.hex()
    await hass.async_block_till_done()


async def test_reconfigure_wrong_server_id_aborts(
    hass: HomeAssistant, fake_server: FakeServer, other_server: FakeServer
) -> None:
    entry = old_address_entry(fake_server)
    await setup_entry_unloaded(hass, entry)
    result = await start_reconfigure(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "127.0.0.1", "port": other_server.port, CONF_KEY: ""}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "server_id_mismatch"
    assert entry.data["host"] == "127.0.0.2"


async def test_reconfigure_cannot_connect(hass: HomeAssistant, fake_server: FakeServer) -> None:
    entry = old_address_entry(fake_server)
    await setup_entry_unloaded(hass, entry)
    result = await start_reconfigure(hass, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"host": "127.0.0.2", "port": fake_server.port, CONF_KEY: ""}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def setup_entry_unloaded(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Add the entry without running its setup (the gateway is not at the stored address)."""
    entry.add_to_hass(hass)


# ------------------------------------------------------------------ options


async def test_options_flow_stores_boolean(hass: HomeAssistant, fake_server: FakeServer) -> None:
    entry = old_address_entry(fake_server)
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(result["flow_id"], {CONF_RELOCATE_MDNS: False})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_RELOCATE_MDNS: False}


async def test_connect_refuses_gateway_with_other_server_id(hass, fake_server, mock_config_entry) -> None:
    """A gateway with a different server id at the stored address is not used."""
    from custom_components.nibe_service_usb.client import ConnectionFailed
    from custom_components.nibe_service_usb.gateway import NibeGateway

    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, "server_id": "00000000-0000-0000-0000-000000000000"},
    )
    gateway = NibeGateway(hass, mock_config_entry)
    with pytest.raises(ConnectionFailed, match="expected"):
        await gateway._connect_once()
    assert not gateway.connected
