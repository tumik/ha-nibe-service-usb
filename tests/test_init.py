"""Integration-level tests: setup, entities, events, commands, reconnect, unload."""

from __future__ import annotations

import asyncio
import copy
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.nibe_service_usb import diagnostics
from custom_components.nibe_service_usb.const import CONF_KEY, DOMAIN

from .fake_server import SERVER_ID, FakeServer


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def eid(hass: HomeAssistant, domain: str, suffix: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{SERVER_ID}_{suffix}")
    assert entity_id is not None, f"no {domain} entity for {suffix}"
    return entity_id


def state(hass: HomeAssistant, domain: str, suffix: str):
    st = hass.states.get(eid(hass, domain, suffix))
    assert st is not None
    return st


async def wait_for(cond: Callable[[], bool | Awaitable[bool]], limit: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + limit
    while True:
        res = cond()
        if asyncio.iscoroutine(res):
            res = await res
        if res:
            return
        assert loop.time() < end, "condition not met in time"
        await asyncio.sleep(0.02)


@pytest.fixture
async def setup_integration(hass: HomeAssistant, mock_config_entry: MockConfigEntry) -> MockConfigEntry:
    await setup_entry(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    return mock_config_entry


async def wait_state(hass: HomeAssistant, domain: str, suffix: str, expected: str) -> None:
    """Events arrive over a real socket, so poll until the state shows up."""
    await wait_for(lambda: state(hass, domain, suffix).state == expected)
    await hass.async_block_till_done()


async def push_status(hass: HomeAssistant, fake: FakeServer, **sections: Any) -> None:
    status = copy.deepcopy(fake.status)
    for key, value in sections.items():
        if isinstance(value, dict) and isinstance(status.get(key), dict):
            status[key].update(value)
        else:
            status[key] = value
    await fake.push_status(status)
    gateway = hass.config_entries.async_entries(DOMAIN)[0].runtime_data
    await wait_for(lambda: gateway.status == status)
    await hass.async_block_till_done()


# ------------------------------------------------------------------ 1. setup


async def test_setup(
    hass: HomeAssistant, fake_server: FakeServer, setup_integration: MockConfigEntry
) -> None:
    entry = setup_integration
    assert entry.state is ConfigEntryState.LOADED
    assert len(fake_server.calls_for("auth")) == 1
    assert len(fake_server.calls_for("subscribe")) == 1
    assert len(fake_server.calls_for("get_status")) == 1
    assert fake_server.calls_for("set_heating_request") == []

    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 1
    device = devices[0]
    assert device.identifiers == {(DOMAIN, SERVER_ID)}
    assert device.manufacturer == "Nibe"
    assert device.model == "F1226-6 R"
    assert device.sw_version == "7994R7"
    assert device.name == "Nibe test"


# ------------------------------------------------------------------ 2. states


async def test_sensor_states(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    st = state(hass, "sensor", "bt1_outdoor")
    assert st.state == "4.7"
    assert st.attributes["unit_of_measurement"] == "°C"
    assert st.attributes["device_class"] == "temperature"
    assert st.attributes["state_class"] == "measurement"

    st = state(hass, "sensor", "degree_minutes")
    assert st.state == "-12.0"
    assert st.attributes["unit_of_measurement"] == "DM"

    st = state(hass, "sensor", "hw_comfort")
    assert st.state == "normal"
    assert st.attributes["options"] == ["eco", "normal", "lux"]
    assert st.attributes["device_class"] == "enum"

    st = state(hass, "sensor", "active_alarms")
    assert st.state == "0"
    assert st.attributes["alarms"] == []

    st = state(hass, "sensor", "comp_hours")
    assert st.attributes["unit_of_measurement"] == "h"
    assert st.attributes["state_class"] == "total_increasing"


async def test_binary_sensor_states(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    st = state(hass, "binary_sensor", "compressor_running")
    assert st.state == "on"
    assert st.attributes["device_class"] == "running"
    assert state(hass, "binary_sensor", "any_alarm").state == "off"
    assert state(hass, "binary_sensor", "status_gateway_connected").state == "on"
    assert state(hass, "binary_sensor", "status_pump_online").state == "on"
    assert state(hass, "binary_sensor", "status_heating_request_active").state == "off"


async def test_registry_properties(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    registry = er.async_get(hass)
    entity_id = eid(hass, "sensor", "set_start_comp")
    reg_entry = registry.async_get(entity_id)
    assert reg_entry is not None
    assert reg_entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(entity_id) is None

    uptime = registry.async_get(eid(hass, "sensor", "uptime"))
    assert uptime is not None
    assert uptime.entity_category is er.EntityCategory.DIAGNOSTIC

    comp_hours = registry.async_get(eid(hass, "sensor", "comp_hours"))
    assert comp_hours is not None
    assert comp_hours.entity_category is er.EntityCategory.DIAGNOSTIC


async def test_status_sensors(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    assert state(hass, "sensor", "status_product").state == "F1226-6 R"
    assert state(hass, "sensor", "status_software_version").state == "7994R7"
    assert state(hass, "sensor", "status_clock_state").state == "unset"
    assert state(hass, "sensor", "status_gateway_version").state == "0.1.0"
    restart = state(hass, "sensor", "status_last_restart")
    assert restart.attributes["device_class"] == "timestamp"
    assert datetime.fromisoformat(restart.state).timestamp() == pytest.approx(1760000000.0)


async def test_select_states(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    assert state(hass, "select", "hot_water_comfort").state == "normal"
    st = state(hass, "select", "heating_request")
    assert st.state == "auto"
    assert st.attributes["options"] == ["auto", "enable", "disable"]
    assert st.attributes["active"] is False
    assert st.attributes["suspended_reason"] is None
    assert st.attributes["last_push"] is None


# ------------------------------------------------------------------ 3. events


async def test_sensor_update_event(
    hass: HomeAssistant, fake_server: FakeServer, setup_integration: MockConfigEntry
) -> None:
    await fake_server.push_sensor_update(
        {"bt1_outdoor": -3.6, "active_alarms": [{"number": 163, "title": "High return"}]}
    )
    await wait_state(hass, "sensor", "active_alarms", "1")
    assert state(hass, "sensor", "bt1_outdoor").state == "-3.6"
    st = state(hass, "sensor", "active_alarms")
    assert st.state == "1"
    assert st.attributes["alarms"] == [{"number": 163, "title": "High return"}]


async def test_pump_offline_and_back(
    hass: HomeAssistant, fake_server: FakeServer, setup_integration: MockConfigEntry
) -> None:
    await push_status(hass, fake_server, pump={"online": False})
    assert state(hass, "sensor", "bt1_outdoor").state == "unavailable"
    assert state(hass, "binary_sensor", "compressor_running").state == "unavailable"
    assert state(hass, "binary_sensor", "status_pump_online").state == "off"
    assert state(hass, "binary_sensor", "status_gateway_connected").state == "on"

    await push_status(hass, fake_server, pump={"online": True})
    assert state(hass, "sensor", "bt1_outdoor").state == "4.7"
    assert state(hass, "binary_sensor", "status_pump_online").state == "on"


async def test_heating_status_event(
    hass: HomeAssistant, fake_server: FakeServer, setup_integration: MockConfigEntry
) -> None:
    await push_status(
        hass, fake_server, heating={"mode": "disable", "active": True, "suspended_reason": None}
    )
    st = state(hass, "select", "heating_request")
    assert st.state == "disable"
    assert st.attributes["active"] is True
    assert state(hass, "binary_sensor", "status_heating_request_active").state == "on"


async def test_unknown_enum_value(
    hass: HomeAssistant, fake_server: FakeServer, setup_integration: MockConfigEntry
) -> None:
    await fake_server.push_sensor_update({"hw_comfort": "weird"})
    await wait_state(hass, "sensor", "hw_comfort", "unknown")


# ------------------------------------------------------------------ 4. commands


async def test_select_hot_water(
    hass: HomeAssistant, fake_server: FakeServer, setup_integration: MockConfigEntry
) -> None:
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": eid(hass, "select", "hot_water_comfort"), "option": "lux"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert fake_server.calls_for("set_hot_water_mode") == [{"mode": "lux"}]
    assert state(hass, "select", "hot_water_comfort").state == "lux"
    assert state(hass, "sensor", "hw_comfort").state == "lux"


async def test_select_heating_request(
    hass: HomeAssistant,
    fake_server: FakeServer,
    setup_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    entry = setup_integration
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": eid(hass, "select", "heating_request"), "option": "disable"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert fake_server.calls_for("set_heating_request") == [{"mode": "disable"}]
    assert state(hass, "select", "heating_request").state == "disable"
    assert state(hass, "binary_sensor", "status_heating_request_active").state == "on"
    key = f"nibe_service_usb.{entry.entry_id}"
    assert hass_storage[key]["data"] == {"heating_request": "disable"}


async def test_invalid_heating_option(
    hass: HomeAssistant, fake_server: FakeServer, setup_integration: MockConfigEntry
) -> None:
    with pytest.raises(Exception):  # noqa: B017, PT011
        await hass.services.async_call(
            "select",
            "select_option",
            {"entity_id": eid(hass, "select", "heating_request"), "option": "bogus"},
            blocking=True,
        )
    assert fake_server.calls_for("set_heating_request") == []


async def test_buttons(
    hass: HomeAssistant, fake_server: FakeServer, setup_integration: MockConfigEntry
) -> None:
    await hass.services.async_call(
        "button", "press", {"entity_id": eid(hass, "button", "hot_water_boost")}, blocking=True
    )
    assert len(fake_server.calls_for("trigger_hot_water_boost")) == 1
    await hass.services.async_call(
        "button", "press", {"entity_id": eid(hass, "button", "refresh")}, blocking=True
    )
    assert len(fake_server.calls_for("refresh")) == 1


async def test_boost_error(
    hass: HomeAssistant, fake_server: FakeServer, setup_integration: MockConfigEntry
) -> None:
    fake_server.boost_error = ("rate_limited", "retry in 500 s")
    with pytest.raises(HomeAssistantError, match="rate_limited"):
        await hass.services.async_call(
            "button", "press", {"entity_id": eid(hass, "button", "hot_water_boost")}, blocking=True
        )


# ------------------------------------------------------------------ 5. reconnect


async def _reopen_listener(fake: FakeServer) -> None:
    """Make the fake listen again on its old port (after the test closed the listener)."""
    fake._server = await asyncio.start_server(fake._on_connect, "127.0.0.1", fake.port)


async def test_reconnect_resends_heating_request(
    hass: HomeAssistant,
    fake_server: FakeServer,
    mock_config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("custom_components.nibe_service_usb.gateway.RECONNECT_MIN", 0.05)
    monkeypatch.setattr("custom_components.nibe_service_usb.gateway.RECONNECT_MAX", 0.2)
    await setup_entry(hass, mock_config_entry)

    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": eid(hass, "select", "heating_request"), "option": "disable"},
        blocking=True,
    )
    assert len(fake_server.calls_for("auth")) == 1

    # Stop listening so the gateway cannot reconnect instantly, then cut the link.
    old_listener = fake_server._server
    assert old_listener is not None
    old_listener.close()
    await fake_server.drop_connections()
    await wait_for(lambda: state(hass, "binary_sensor", "status_gateway_connected").state == "off")
    assert state(hass, "sensor", "bt1_outdoor").state == "unavailable"

    await _reopen_listener(fake_server)
    await wait_for(lambda: len(fake_server.authenticated_connections) == 1)
    await wait_for(lambda: state(hass, "binary_sensor", "status_gateway_connected").state == "on")
    await hass.async_block_till_done()

    assert len(fake_server.calls_for("auth")) >= 2
    last_auth = max(i for i, (cmd, _) in enumerate(fake_server.calls) if cmd == "auth")
    last_set = max(i for i, (cmd, _) in enumerate(fake_server.calls) if cmd == "set_heating_request")
    assert last_set > last_auth
    assert fake_server.calls_for("set_heating_request")[-1] == {"mode": "disable"}
    assert state(hass, "sensor", "bt1_outdoor").state == "4.7"
    assert state(hass, "select", "heating_request").state == "disable"
    await old_listener.wait_closed()


async def test_stored_heating_request_resent_on_setup(
    hass: HomeAssistant,
    fake_server: FakeServer,
    mock_config_entry: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    key = f"nibe_service_usb.{mock_config_entry.entry_id}"
    hass_storage[key] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": {"heating_request": "enable"},
    }
    await setup_entry(hass, mock_config_entry)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert fake_server.calls_for("set_heating_request") == [{"mode": "enable"}]
    assert state(hass, "select", "heating_request").state == "enable"


# ------------------------------------------------------------------ 6. heartbeat


async def test_heartbeat(
    hass: HomeAssistant,
    fake_server: FakeServer,
    mock_config_entry: MockConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("custom_components.nibe_service_usb.gateway.PING_INTERVAL", 0.05)
    await setup_entry(hass, mock_config_entry)
    await wait_for(lambda: len(fake_server.calls_for("ping")) >= 2)
    assert mock_config_entry.state is ConfigEntryState.LOADED


# ------------------------------------------------------------------ 7. setup failures


async def test_setup_retry_when_unreachable(
    hass: HomeAssistant, fake_server: FakeServer, mock_config_entry: MockConfigEntry
) -> None:
    await fake_server.stop()
    mock_config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_wrong_key_starts_reauth(hass: HomeAssistant, fake_server: FakeServer) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=SERVER_ID,
        title="Nibe test",
        data={"host": "127.0.0.1", "port": fake_server.port, CONF_KEY: "ff" * 32, "server_id": SERVER_ID},
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == "reauth"


# ------------------------------------------------------------------ 8. unload / remove


async def test_unload(
    hass: HomeAssistant, fake_server: FakeServer, setup_integration: MockConfigEntry
) -> None:
    entry = setup_integration
    assert len(fake_server.authenticated_connections) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED
    await wait_for(lambda: len(fake_server.authenticated_connections) == 0)
    assert state(hass, "sensor", "bt1_outdoor").state == "unavailable"


async def test_remove_entry_clears_storage(
    hass: HomeAssistant,
    fake_server: FakeServer,
    setup_integration: MockConfigEntry,
    hass_storage: dict[str, Any],
) -> None:
    entry = setup_integration
    key = f"nibe_service_usb.{entry.entry_id}"
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": eid(hass, "select", "heating_request"), "option": "enable"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert key in hass_storage
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert key not in hass_storage
    await wait_for(lambda: len(fake_server.authenticated_connections) == 0)


# ------------------------------------------------------------------ 9. diagnostics


async def test_diagnostics(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    data = await diagnostics.async_get_config_entry_diagnostics(hass, setup_integration)
    assert data["entry"][CONF_KEY] == "**REDACTED**"
    assert data["entry"]["host"] == "127.0.0.1"
    assert data["connected"] is True
    assert data["status"]["identity"]["product"] == "F1226-6 R"
    assert "bt1_outdoor" in data["sensors"]
    assert data["values"]["bt1_outdoor"] == 4.7
    assert data["server"]["server_id"] == SERVER_ID


# ------------------------------------------------------------------ 10. dynamic sensors


async def test_dynamic_sensor(hass: HomeAssistant, setup_integration: MockConfigEntry) -> None:
    registry = er.async_get(hass)
    assert registry.async_get_entity_id("sensor", DOMAIN, f"{SERVER_ID}_new_temp") is None
    setup_integration.runtime_data._apply_snapshot(
        {
            "new_temp": {
                "value": 1.5,
                "ts": 1.0,
                "name": "New temp",
                "kind": "temperature",
                "unit": "°C",
                "precision": 1,
                "tier": "core",
                "device_class": "temperature",
                "state_class": "measurement",
                "description": None,
            }
        }
    )
    await hass.async_block_till_done()
    assert state(hass, "sensor", "new_temp").state == "1.5"
