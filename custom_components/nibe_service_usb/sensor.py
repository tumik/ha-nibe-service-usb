"""Sensors: one per server-side sensor plus diagnostics derived from get_status."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
    UnitOfPower,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DIAGNOSTIC_TIERS, DISABLED_TIERS
from .entity import NibeEntity, NibeSensorEntityMixin
from .gateway import NibeConfigEntry, NibeGateway

PARALLEL_UPDATES = 0

SENSOR_KINDS = frozenset({"temperature", "number", "duration", "enum", "alarm_list"})

UNIT_MAP: dict[str, str] = {
    "°C": UnitOfTemperature.CELSIUS,
    "min": UnitOfTime.MINUTES,
    "s": UnitOfTime.SECONDS,
    "h": UnitOfTime.HOURS,
    "d": UnitOfTime.DAYS,
    "%": PERCENTAGE,
    "kW": UnitOfPower.KILO_WATT,
}


def _enum_or_none[T](enum_cls: type[T], value: Any) -> T | None:
    if not value:
        return None
    try:
        return enum_cls(value)  # type: ignore[call-arg]
    except ValueError:
        return None


class NibeSensor(NibeSensorEntityMixin, SensorEntity):
    """A sensor published by the gateway."""

    def __init__(self, gateway: NibeGateway, sensor: str) -> None:
        super().__init__(gateway, sensor)
        meta = self.meta
        kind = meta.get("kind")
        self._kind = kind
        tier = meta.get("tier")
        if tier in DIAGNOSTIC_TIERS:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        if tier in DISABLED_TIERS:
            self._attr_entity_registry_enabled_default = False

        if kind == "enum":
            options = meta.get("options") or []
            self._options = [str(o) for o in (options.values() if isinstance(options, dict) else options)]
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = self._options
            return

        unit = meta.get("unit")
        if unit:
            self._attr_native_unit_of_measurement = UNIT_MAP.get(unit, unit)
        if (device_class := _enum_or_none(SensorDeviceClass, meta.get("device_class"))) is not None:
            self._attr_device_class = device_class
        if kind == "alarm_list":
            self._attr_native_unit_of_measurement = None
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif (state_class := _enum_or_none(SensorStateClass, meta.get("state_class"))) is not None:
            self._attr_state_class = state_class
        if (precision := meta.get("precision")) is not None:
            self._attr_suggested_display_precision = int(precision)

    @property
    def native_value(self) -> Any:
        value = self.value
        if self._kind == "alarm_list":
            return len(value) if isinstance(value, list) else 0
        if self._kind == "enum":
            return value if value in self._options else None
        return value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self._kind == "alarm_list":
            value = self.value
            return {"alarms": value if isinstance(value, list) else []}
        return None


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return dt_util.utc_from_timestamp(value)


def _text(value: Any) -> str | None:
    return None if value is None or value == "" else str(value)


@dataclass(frozen=True)
class NibeStatusSensorDescription:
    """A diagnostic sensor read from the get_status document."""

    key: str
    path: tuple[str, ...]
    device_class: SensorDeviceClass | None = None
    unit: str | None = None
    state_class: SensorStateClass | None = None
    options: tuple[str, ...] | None = None
    transform: Callable[[Any], Any] | None = None


STATUS_SENSORS: tuple[NibeStatusSensorDescription, ...] = (
    NibeStatusSensorDescription("product", ("identity", "product"), transform=_text),
    NibeStatusSensorDescription("software_version", ("identity", "software_version"), transform=_text),
    NibeStatusSensorDescription("product_code", ("identity", "product_code"), transform=_text),
    NibeStatusSensorDescription(
        "clock_state",
        ("identity", "clock_state"),
        device_class=SensorDeviceClass.ENUM,
        options=("unset", "ok", "drifting"),
        transform=lambda v: None if v is None else str(v).lower(),
    ),
    NibeStatusSensorDescription(
        "clock_drift",
        ("identity", "clock_drift"),
        device_class=SensorDeviceClass.DURATION,
        unit=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    NibeStatusSensorDescription(
        "last_restart",
        ("identity", "last_restart"),
        device_class=SensorDeviceClass.TIMESTAMP,
        transform=_timestamp,
    ),
    NibeStatusSensorDescription(
        "last_health_check",
        ("identity", "last_health_check"),
        device_class=SensorDeviceClass.TIMESTAMP,
        transform=_timestamp,
    ),
    NibeStatusSensorDescription("gateway_version", ("version",), transform=_text),
    NibeStatusSensorDescription(
        "gateway_uptime",
        ("uptime",),
        device_class=SensorDeviceClass.DURATION,
        unit=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    NibeStatusSensorDescription("clients", ("clients",), state_class=SensorStateClass.MEASUREMENT),
    NibeStatusSensorDescription(
        "last_heartbeat",
        ("watchdog", "last_heartbeat"),
        device_class=SensorDeviceClass.TIMESTAMP,
        transform=_timestamp,
    ),
    NibeStatusSensorDescription(
        "reverted_at",
        ("watchdog", "reverted_at"),
        device_class=SensorDeviceClass.TIMESTAMP,
        transform=_timestamp,
    ),
    NibeStatusSensorDescription(
        "heating_target_dm",
        ("heating", "target_dm"),
        unit="DM",
        state_class=SensorStateClass.MEASUREMENT,
    ),
    NibeStatusSensorDescription(
        "heating_current_dm",
        ("heating", "current_dm"),
        unit="DM",
        state_class=SensorStateClass.MEASUREMENT,
    ),
)


def status_lookup(status: dict[str, Any], path: tuple[str, ...]) -> Any:
    """Walk ``path`` through nested dicts; None when any section is missing."""
    node: Any = status
    for part in path:
        if not isinstance(node, dict):
            return None
        node = node.get(part)
    return node


class NibeStatusSensor(NibeEntity, SensorEntity):
    """Diagnostic sensor derived from the status document."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, gateway: NibeGateway, description: NibeStatusSensorDescription) -> None:
        super().__init__(gateway, f"status_{description.key}", None)
        self.description = description
        self._attr_translation_key = description.key
        self._attr_device_class = description.device_class
        self._attr_native_unit_of_measurement = description.unit
        self._attr_state_class = description.state_class
        if description.options is not None:
            self._attr_options = list(description.options)

    @property
    def native_value(self) -> Any:
        desc = self.description
        value = status_lookup(self.gateway.status, desc.path)
        if (
            desc.key == "clock_drift"
            and status_lookup(self.gateway.status, ("identity", "clock_state")) == "unset"
        ):
            # The pump counts from 2010-01-01 until its clock is set; the drift is meaningless then.
            return None
        if desc.transform is not None:
            value = desc.transform(value)
        if desc.options is not None and value not in desc.options:
            return None
        return value


def _new_entities(gateway: NibeGateway, names: list[str]) -> list[NibeSensor]:
    return [
        NibeSensor(gateway, name)
        for name in names
        if name in gateway.sensors and gateway.sensors[name].get("kind") in SENSOR_KINDS
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
            *(NibeStatusSensor(gateway, d) for d in STATUS_SENSORS),
        ]
    )
    entry.async_on_unload(gateway.async_add_new_sensor_listener(add_sensors))
