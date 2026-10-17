"""Find a gateway again after its address changed.

The gateway is identified by its server id, the ``id`` TXT property of its mDNS
announcement. Nothing here connects to or sends anything to a candidate address.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from .const import ZEROCONF_TYPE

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

type Target = tuple[str, int]


# ------------------------------------------------------------------ mDNS


def _txt_id(properties: dict[Any, Any] | None) -> str | None:
    for key, value in (properties or {}).items():
        name = key.decode("utf-8", "replace") if isinstance(key, bytes) else str(key)
        if name.lower() == "id" and value is not None:
            return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
    return None


def pick_service(infos: Iterable[Any], server_id: str) -> Target | None:
    """Return ``(ipv4, port)`` of the first resolved service whose TXT ``id`` is ``server_id``.

    ``infos`` are ``zeroconf.ServiceInfo`` objects (anything with ``properties``,
    ``port`` and ``parsed_addresses(version)``).
    """
    from zeroconf import IPVersion

    for info in infos:
        if info is None or _txt_id(getattr(info, "properties", None)) != server_id:
            continue
        addresses = info.parsed_addresses(IPVersion.V4Only)
        if addresses and info.port:
            return addresses[0], int(info.port)
    return None


async def find_by_mdns(hass: HomeAssistant, server_id: str, timeout: float = 4.0) -> Target | None:
    """Look for the gateway with ``server_id`` through Home Assistant's shared Zeroconf."""
    try:
        return await _find_by_mdns(hass, server_id, timeout)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("mDNS search for %s failed: %s", server_id, err)
        return None


async def _find_by_mdns(hass: HomeAssistant, server_id: str, timeout: float) -> Target | None:
    from homeassistant.components.zeroconf import async_get_async_instance
    from zeroconf import ServiceStateChange
    from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo

    aiozc = await async_get_async_instance(hass)
    zc = aiozc.zeroconf

    # Whatever is already cached.
    suffix = ZEROCONF_TYPE.lower()
    cached = []
    for name in list(zc.cache.names()):
        if name.lower().endswith(suffix) and name.lower() != suffix:
            info = AsyncServiceInfo(ZEROCONF_TYPE, name)
            if info.load_from_cache(zc):
                cached.append(info)
    found = pick_service(cached, server_id)
    if found is not None:
        return found

    names: set[str] = set()

    def on_change(
        zeroconf: Any, service_type: str, name: str, state_change: ServiceStateChange, **_: Any
    ) -> None:
        if state_change is not ServiceStateChange.Removed:
            names.add(name)

    browser = AsyncServiceBrowser(zc, ZEROCONF_TYPE, handlers=[on_change])
    try:
        await asyncio.sleep(timeout / 2)
        infos = [AsyncServiceInfo(ZEROCONF_TYPE, name) for name in sorted(names)]
        if not infos:
            return None
        await asyncio.gather(*(info.async_request(zc, 3000) for info in infos), return_exceptions=True)
        return pick_service(infos, server_id)
    finally:
        with contextlib.suppress(Exception):
            await browser.async_cancel()
