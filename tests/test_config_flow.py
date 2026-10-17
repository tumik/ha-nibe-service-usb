"""Config flow tests (user, zeroconf, reauth) against the fake gateway."""

from __future__ import annotations

import base64
import socket
from collections.abc import Generator
from ipaddress import ip_address
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import SOURCE_REAUTH, SOURCE_USER, SOURCE_ZEROCONF
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nibe_service_usb.const import CONF_KEY, CONF_SERVER_ID, DOMAIN

from .fake_server import SERVER_ID, TEST_KEY, FakeServer


@pytest.fixture(autouse=True)
def mock_setup_entry() -> Generator[AsyncMock]:
    """Keep the platforms (and the gateway) out of the flow tests."""
    with (
        patch("custom_components.nibe_service_usb.async_setup_entry", return_value=True) as setup,
        patch("custom_components.nibe_service_usb.async_unload_entry", return_value=True),
    ):
        yield setup


def grouped(key: bytes) -> str:
    text = base64.b32encode(key).decode("ascii").rstrip("=")
    return "-".join(text[i : i + 8] for i in range(0, len(text), 8))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def zeroconf_info(port: int, **props: str | None) -> ZeroconfServiceInfo:
    properties: dict[str, str | None] = {
        "id": SERVER_ID,
        "proto": "1",
        "ver": "0.1.0",
        "auth": "hmac-sha256",
        "name": "Garage pump",
    }
    for key, value in props.items():
        if value is None:
            properties.pop(key, None)
        else:
            properties[key] = value
    return ZeroconfServiceInfo(
        ip_address=ip_address("127.0.0.1"),
        ip_addresses=[ip_address("127.0.0.1")],
        hostname="nibe-6f1c2a9e.local.",
        name="Garage pump._nibe-usb._tcp.local.",
        port=port,
        properties=properties,
        type="_nibe-usb._tcp.local.",
    )


def user_input(fake_server: FakeServer, key: str = TEST_KEY.hex(), **overrides) -> dict:
    return {"host": "127.0.0.1", "port": fake_server.port, CONF_KEY: key, **overrides}


# ------------------------------------------------------------------ user flow


async def test_user_flow_creates_entry(
    hass: HomeAssistant, fake_server: FakeServer, mock_setup_entry: AsyncMock
) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert not result["errors"]

    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input(fake_server))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert "F1226-6 R" in result["title"]
    assert result["data"] == {
        "host": "127.0.0.1",
        "port": fake_server.port,
        CONF_KEY: TEST_KEY.hex(),
        CONF_SERVER_ID: SERVER_ID,
    }
    entry = result["result"]
    assert entry.unique_id == SERVER_ID
    await hass.async_block_till_done()
    assert len(mock_setup_entry.mock_calls) == 1
    # The validation handshake went through auth, ping and get_status.
    assert [c for c, _ in fake_server.calls] == ["auth", "ping", "get_status"]
    await _wait_no_connections(fake_server)


async def test_user_flow_normalises_grouped_base32_key(hass: HomeAssistant, fake_server: FakeServer) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input(fake_server, key=grouped(TEST_KEY))
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_KEY] == TEST_KEY.hex()


async def test_user_flow_strips_host_whitespace(hass: HomeAssistant, fake_server: FakeServer) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input(fake_server, host=" 127.0.0.1 ")
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["host"] == "127.0.0.1"


@pytest.mark.parametrize(
    ("key", "port_unused", "errors"),
    [
        (TEST_KEY.hex(), True, {"base": "cannot_connect"}),
        ("00" * 32, False, {"base": "invalid_auth"}),
        ("this is garbage", False, {"key": "invalid_key"}),
    ],
    ids=["cannot_connect", "invalid_auth", "invalid_key"],
)
async def test_user_flow_errors_then_recovery(
    hass: HomeAssistant,
    fake_server: FakeServer,
    key: str,
    port_unused: bool,
    errors: dict[str, str],
) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    bad = user_input(fake_server, key=key)
    if port_unused:
        bad["port"] = free_port()
    result = await hass.config_entries.flow.async_configure(result["flow_id"], bad)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == errors

    # The user can fix the input and finish the same flow.
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input(fake_server))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_KEY] == TEST_KEY.hex()


async def test_user_flow_unsupported_proto(hass: HomeAssistant, fake_server: FakeServer) -> None:
    fake_server.proto = 2
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input(fake_server))
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unsupported_proto"}


async def test_user_flow_wrong_server_proof_is_invalid_auth(
    hass: HomeAssistant, fake_server: FakeServer
) -> None:
    fake_server.wrong_server_proof = True
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input(fake_server))
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_already_configured_updates_host_and_port(
    hass: HomeAssistant, fake_server: FakeServer
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SERVER_ID,
        title="Nibe test",
        data={"host": "192.0.2.10", "port": 1234, CONF_KEY: TEST_KEY.hex(), CONF_SERVER_ID: SERVER_ID},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input(fake_server))
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data["host"] == "127.0.0.1"
    assert entry.data["port"] == fake_server.port
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


# ------------------------------------------------------------------ zeroconf


async def test_zeroconf_flow_creates_entry(
    hass: HomeAssistant, fake_server: FakeServer, mock_setup_entry: AsyncMock
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=zeroconf_info(fake_server.port)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"
    assert not result["errors"]
    # Discovery alone must not connect (no key yet).
    assert fake_server.calls == []

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_KEY: TEST_KEY.hex()})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Garage pump"
    assert result["data"] == {
        "host": "127.0.0.1",
        "port": fake_server.port,
        CONF_KEY: TEST_KEY.hex(),
        CONF_SERVER_ID: SERVER_ID,
    }
    assert result["result"].unique_id == SERVER_ID
    await hass.async_block_till_done()
    assert len(mock_setup_entry.mock_calls) == 1


async def test_zeroconf_flow_base32_key_and_errors(hass: HomeAssistant, fake_server: FakeServer) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=zeroconf_info(fake_server.port)
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_KEY: "nonsense"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"
    assert result["errors"] == {"key": "invalid_key"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_KEY: "11" * 32})
    assert result["errors"] == {"base": "invalid_auth"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_KEY: grouped(TEST_KEY)})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_KEY] == TEST_KEY.hex()


async def test_zeroconf_cannot_connect(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=zeroconf_info(free_port())
    )
    assert result["step_id"] == "zeroconf_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_KEY: TEST_KEY.hex()})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_zeroconf_server_id_mismatch_aborts(hass: HomeAssistant, fake_server: FakeServer) -> None:
    other_id = "11111111-2222-3333-4444-555555555555"
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=zeroconf_info(fake_server.port, id=other_id)
    )
    assert result["step_id"] == "zeroconf_confirm"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_KEY: TEST_KEY.hex()})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "server_id_mismatch"


async def test_zeroconf_unsupported_proto_aborts(hass: HomeAssistant, fake_server: FakeServer) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=zeroconf_info(fake_server.port, proto="2")
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unsupported_proto"


async def test_zeroconf_without_id_aborts(hass: HomeAssistant, fake_server: FakeServer) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=zeroconf_info(fake_server.port, id=None)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_server_id"


async def test_zeroconf_without_name_property_uses_service_name(
    hass: HomeAssistant, fake_server: FakeServer
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=zeroconf_info(fake_server.port, name=None)
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_KEY: TEST_KEY.hex()})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Garage pump"


async def test_zeroconf_already_configured_updates_port(hass: HomeAssistant, fake_server: FakeServer) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SERVER_ID,
        title="Nibe test",
        data={"host": "127.0.0.1", "port": 9477, CONF_KEY: TEST_KEY.hex(), CONF_SERVER_ID: SERVER_ID},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_ZEROCONF}, data=zeroconf_info(fake_server.port)
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data["port"] == fake_server.port
    assert entry.data[CONF_KEY] == TEST_KEY.hex()
    await hass.async_block_till_done()


# ------------------------------------------------------------------ reauth


def _reauth_entry(hass: HomeAssistant, fake_server: FakeServer, key: str = "ab" * 32) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SERVER_ID,
        title="Nibe test",
        data={"host": "127.0.0.1", "port": fake_server.port, CONF_KEY: key, CONF_SERVER_ID: SERVER_ID},
    )
    entry.add_to_hass(hass)
    return entry


async def _start_reauth(hass: HomeAssistant, entry: MockConfigEntry):
    return await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_REAUTH, "entry_id": entry.entry_id, "unique_id": entry.unique_id},
        data=entry.data,
    )


async def test_reauth_flow_updates_key(hass: HomeAssistant, fake_server: FakeServer) -> None:
    entry = _reauth_entry(hass, fake_server)
    result = await _start_reauth(hass, entry)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_KEY: grouped(TEST_KEY)})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_KEY] == TEST_KEY.hex()
    assert entry.data["host"] == "127.0.0.1"
    assert entry.data["port"] == fake_server.port
    await hass.async_block_till_done()


async def test_reauth_flow_wrong_key_shows_error(hass: HomeAssistant, fake_server: FakeServer) -> None:
    entry = _reauth_entry(hass, fake_server)
    result = await _start_reauth(hass, entry)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_KEY: "cd" * 32})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data[CONF_KEY] == "ab" * 32

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_KEY: TEST_KEY.hex()})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_KEY] == TEST_KEY.hex()
    await hass.async_block_till_done()


async def test_reauth_via_async_start_reauth(hass: HomeAssistant, fake_server: FakeServer) -> None:
    entry = _reauth_entry(hass, fake_server)
    entry.async_start_reauth(hass)
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == SOURCE_REAUTH
    assert flows[0]["step_id"] == "reauth_confirm"


async def test_reauth_server_id_mismatch_aborts(hass: HomeAssistant, fake_server: FakeServer) -> None:
    entry = _reauth_entry(hass, fake_server)
    fake_server.server_id = "99999999-9999-9999-9999-999999999999"
    result = await _start_reauth(hass, entry)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_KEY: TEST_KEY.hex()})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "server_id_mismatch"
    assert entry.data[CONF_KEY] == "ab" * 32


async def _wait_no_connections(fake_server: FakeServer) -> None:
    import asyncio

    async with asyncio.timeout(2):
        while fake_server.connections:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
