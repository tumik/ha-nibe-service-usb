"""Buttons: start a periodic hot water increase, force a sensor refresh."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .command import run_command
from .entity import NibeEntity
from .gateway import NibeConfigEntry, NibeGateway

PARALLEL_UPDATES = 1


class NibeHotWaterBoostButton(NibeEntity, ButtonEntity):
    _attr_translation_key = "hot_water_boost"

    def __init__(self, gateway: NibeGateway) -> None:
        super().__init__(gateway, "hot_water_boost", None)

    async def async_press(self) -> None:
        await run_command(
            "Starting periodic hot water increase", self.gateway.async_trigger_hot_water_boost()
        )


class NibeRefreshButton(NibeEntity, ButtonEntity):
    _attr_translation_key = "refresh"

    def __init__(self, gateway: NibeGateway) -> None:
        super().__init__(gateway, "refresh", None)

    async def async_press(self) -> None:
        await run_command("Refreshing sensors", self.gateway.async_refresh())


async def async_setup_entry(
    hass: HomeAssistant, entry: NibeConfigEntry, async_add_entities: AddConfigEntryEntitiesCallback
) -> None:
    gateway = entry.runtime_data
    async_add_entities([NibeHotWaterBoostButton(gateway), NibeRefreshButton(gateway)])
