"""Select entities: hot water comfort mode and the heating request."""

from __future__ import annotations

from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .command import run_command
from .entity import NibeEntity
from .gateway import STATUS_KEY, NibeConfigEntry, NibeGateway
from .sensor import status_lookup

PARALLEL_UPDATES = 1

HOT_WATER_OPTIONS = ["eco", "normal", "lux"]
HEATING_OPTIONS = ["auto", "enable", "disable"]


def _iso(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return dt_util.utc_from_timestamp(value).isoformat()


class NibeHotWaterComfortSelect(NibeEntity, SelectEntity):
    """Hot water comfort mode of the pump."""

    _attr_translation_key = "hot_water_comfort"
    _attr_options = HOT_WATER_OPTIONS

    def __init__(self, gateway: NibeGateway) -> None:
        super().__init__(gateway, "hot_water_comfort", ["hw_comfort", STATUS_KEY])

    @property
    def current_option(self) -> str | None:
        value = self.gateway.values.get("hw_comfort")
        if value in HOT_WATER_OPTIONS:
            return value
        mode = status_lookup(self.gateway.status, ("hot_water", "mode"))
        return mode if mode in HOT_WATER_OPTIONS else None

    async def async_select_option(self, option: str) -> None:
        await run_command("Setting hot water mode", self.gateway.async_set_hot_water_mode(option))


class NibeHeatingRequestSelect(NibeEntity, SelectEntity):
    """The heating request sent to the gateway (auto / enable / disable)."""

    _attr_translation_key = "heating_request"
    _attr_options = HEATING_OPTIONS

    def __init__(self, gateway: NibeGateway) -> None:
        super().__init__(gateway, "heating_request", None)

    @property
    def current_option(self) -> str:
        mode = status_lookup(self.gateway.status, ("heating", "mode"))
        return mode if mode in HEATING_OPTIONS else "auto"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self.gateway.status

        def heating(key: str) -> Any:
            return status_lookup(status, ("heating", key))

        return {
            "active": heating("active"),
            "suspended_reason": heating("suspended_reason"),
            "target_dm": heating("target_dm"),
            "current_dm": heating("current_dm"),
            "last_push": _iso(heating("last_push")),
            "next_push": _iso(heating("next_push")),
        }

    async def async_select_option(self, option: str) -> None:
        await run_command("Setting heating request", self.gateway.async_set_heating_request(option))


async def async_setup_entry(
    hass: HomeAssistant, entry: NibeConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    gateway = entry.runtime_data
    async_add_entities([NibeHotWaterComfortSelect(gateway), NibeHeatingRequestSelect(gateway)])
