"""Diagnostics for the nibe-service-usb integration."""

from __future__ import annotations

import dataclasses
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_KEY
from .gateway import NibeConfigEntry

TO_REDACT = {CONF_KEY}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: NibeConfigEntry) -> dict[str, Any]:
    gateway = entry.runtime_data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "server": dataclasses.asdict(gateway.server) if gateway.server is not None else None,
        "connected": gateway.connected,
        "poll_interval": gateway.poll_interval,
        "heating_request": gateway.heating_request,
        "status": gateway.status,
        "sensors": gateway.sensors,
        "values": gateway.values,
    }
