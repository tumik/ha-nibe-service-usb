"""Home Assistant integration for the nibe-service-usb heat pump gateway."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import PLATFORMS
from .gateway import NibeConfigEntry, NibeGateway


async def async_setup_entry(hass: HomeAssistant, entry: NibeConfigEntry) -> bool:
    gateway = NibeGateway(hass, entry)
    await gateway.async_start()
    entry.runtime_data = gateway
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: NibeConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    await entry.runtime_data.async_stop()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Forget the persisted heating request when the entry is deleted."""
    await NibeGateway(hass, entry).async_remove_storage()
