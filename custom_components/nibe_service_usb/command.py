"""Helper that turns client errors into Home Assistant errors."""

from __future__ import annotations

from collections.abc import Awaitable
from typing import Any

from homeassistant.exceptions import HomeAssistantError

from .client import CommandError, NibeError


async def run_command(action: str, command: Awaitable[Any]) -> Any:
    """Await a gateway command; raise HomeAssistantError with a readable message on failure."""
    try:
        return await command
    except CommandError as err:
        detail = f"{err.message} ({err.code})" if err.message else err.code
        raise HomeAssistantError(f"{action} failed: {detail}") from err
    except NibeError as err:
        raise HomeAssistantError(f"{action} failed: {err}") from err
