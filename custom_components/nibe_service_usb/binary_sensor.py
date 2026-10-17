"""Binary sensors: server-side binary sensors plus connection and status flags."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DIAGNOSTIC_TIERS, DISABLED_TIERS
from .entity import NibeEntity, NibeSensorEntityMixin
from .gateway import NibeConfigEntry, NibeGateway
from .sensor import status_lookup

PARALLEL_UPDATES = 0


class NibeBinarySensor(NibeSensorEntityMixin, BinarySensorEntity):
    """A binary sensor published by the gateway."""

    def __init__(self, gateway: NibeGateway, sensor: str) -> None:
        super().__init__(gateway, sensor)
        try:
            self._attr_device_class = BinarySensorDeviceClass(self.meta.get("device_class"))
        except ValueError:
            pass
        tier = self.meta.get("tier")
        if tier in DIAGNOSTIC_TIERS:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        if tier in DISABLED_TIERS:
            self._attr_entity_registry_enabled_default = False

    @property
    def is_on(self) -> bool | None:
        value = self.value
        return None if value is None else bool(value)


@dataclass(frozen=True)
class NibeStatusBinarySensorDescription:
    """A binary sensor read from the get_status document."""

    key: str
    path: tuple[str, ...] = ()
    device_class: BinarySensorDeviceClass | None = None
    diagnostic: bool = False
    attributes: Callable[[dict[str, Any]], dict[str, Any]] | None = None


def _heating_attributes(status: dict[str, Any]) -> dict[str, Any]:
    return {"suspended_reason": status_lookup(status, ("heating", "suspended_reason"))}


def _periodic_attributes(status: dict[str, Any]) -> dict[str, Any]:
    increase = ("hot_water", "periodic_increase")
    return {
        "remaining_seconds": status_lookup(status, (*increase, "remaining_seconds")),
        "hold": status_lookup(status, (*increase, "hold")),
    }


STATUS_BINARY_SENSORS: tuple[NibeStatusBinarySensorDescription, ...] = (
    NibeStatusBinarySensorDescription(
        "gateway_connected", device_class=BinarySensorDeviceClass.CONNECTIVITY, diagnostic=True
    ),
    NibeStatusBinarySensorDescription(
        "pump_online",
        ("pump", "online"),
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        diagnostic=True,
    ),
    NibeStatusBinarySensorDescription("watchdog_alive", ("watchdog", "ha_alive"), diagnostic=True),
    NibeStatusBinarySensorDescription(
        "heating_request_active", ("heating", "active"), attributes=_heating_attributes
    ),
    NibeStatusBinarySensorDescription(
        "eeprom_write_failed",
        ("identity", "eeprom_write_failed"),
        device_class=BinarySensorDeviceClass.PROBLEM,
        diagnostic=True,
    ),
    NibeStatusBinarySensorDescription(
        "periodic_increase_running",
        ("hot_water", "periodic_increase", "running"),
        device_class=BinarySensorDeviceClass.RUNNING,
        attributes=_periodic_attributes,
    ),
)


class NibeStatusBinarySensor(NibeEntity, BinarySensorEntity):
    """Binary sensor derived from the status document or the connection state."""

    def __init__(self, gateway: NibeGateway, description: NibeStatusBinarySensorDescription) -> None:
        super().__init__(gateway, f"status_{description.key}", None)
        self.description = description
        self._attr_translation_key = description.key
        self._attr_device_class = description.device_class
        if description.diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def available(self) -> bool:
        if self.description.key == "gateway_connected":
            return True
        return self.gateway.connected

    @property
    def is_on(self) -> bool | None:
        if self.description.key == "gateway_connected":
            return self.gateway.connected
        value = status_lookup(self.gateway.status, self.description.path)
        return None if value is None else bool(value)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.description.attributes is None:
            return None
        return self.description.attributes(self.gateway.status)


def _new_entities(gateway: NibeGateway, names: list[str]) -> list[NibeBinarySensor]:
    return [
        NibeBinarySensor(gateway, name)
        for name in names
        if name in gateway.sensors and gateway.sensors[name].get("kind") == "binary"
    ]


async def async_setup_entry(
    hass: HomeAssistant, entry: NibeConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    gateway = entry.runtime_data

    def add_sensors(names: list[str]) -> None:
        if entities := _new_entities(gateway, names):
            async_add_entities(entities)

    async_add_entities(
        [
            *_new_entities(gateway, list(gateway.sensors)),
            *(NibeStatusBinarySensor(gateway, d) for d in STATUS_BINARY_SENSORS),
        ]
    )
    entry.async_on_unload(gateway.async_add_new_sensor_listener(add_sensors))
