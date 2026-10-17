"""Base entity: device info, availability and change subscription."""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN, MANUFACTURER
from .gateway import NibeGateway


class NibeEntity(Entity):
    """Entity backed by the gateway's push state."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, gateway: NibeGateway, unique_suffix: str, watch: Iterable[str] | None) -> None:
        self.gateway = gateway
        self._watch = None if watch is None else list(watch)
        self._attr_unique_id = f"{gateway.server_id}_{unique_suffix}"

    @property
    def device_info(self) -> DeviceInfo:
        identity = self.gateway.identity
        server = self.gateway.server
        info = DeviceInfo(
            identifiers={(DOMAIN, self.gateway.server_id)},
            manufacturer=MANUFACTURER,
            name=self.gateway.entry.title,
            configuration_url=None,
        )
        if identity.get("product"):
            info["model"] = str(identity["product"])
        if identity.get("software_version"):
            info["sw_version"] = str(identity["software_version"])
        if identity.get("product_code"):
            info["serial_number"] = str(identity["product_code"])
        if server is not None and server.version:
            info["hw_version"] = f"nibe-service-usb {server.version}"
        return info

    @property
    def available(self) -> bool:
        return self.gateway.connected

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self.gateway.async_add_listener(self._watch, self._handle_update))

    def _handle_update(self) -> None:
        self.async_write_ha_state()


class NibeSensorEntityMixin(NibeEntity):
    """Entity for one server-side sensor; unavailable while the pump is offline or the value is null."""

    def __init__(self, gateway: NibeGateway, sensor: str) -> None:
        super().__init__(gateway, sensor, [sensor])
        self.sensor = sensor
        self.meta = gateway.sensors[sensor]
        self._attr_name = str(self.meta.get("name") or sensor)

    @property
    def value(self):  # noqa: ANN201
        return self.gateway.values.get(self.sensor)

    @property
    def available(self) -> bool:
        return self.gateway.connected and self.gateway.pump_online and self.value is not None
