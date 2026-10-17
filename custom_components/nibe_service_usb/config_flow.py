"""Config flow: zeroconf discovery, manual host/port/key entry and re-authentication."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .client import (
    PROTO,
    AuthenticationError,
    NibeError,
    NibeServiceUsbClient,
    ProtocolError,
    parse_key,
)
from .const import (
    CONF_KEY,
    CONF_RELOCATE_MDNS,
    CONF_SERVER_ID,
    DEFAULT_PORT_VALUE,
    DOMAIN,
)
from .gateway import client_name

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT_VALUE): vol.All(vol.Coerce(int), vol.Range(1, 65535)),
        vol.Required(CONF_KEY): str,
    }
)
KEY_SCHEMA = vol.Schema({vol.Required(CONF_KEY): str})


@dataclass
class Validated:
    server_id: str
    key_hex: str
    product: str | None


async def async_validate(host: str, port: int, key_text: str) -> Validated:
    """Run the full handshake plus ``ping`` and ``get_status``; raises client errors."""
    key = parse_key(key_text)
    client = NibeServiceUsbClient(host, port, key, client_name=client_name())
    try:
        info = await client.connect()
        await client.ping()
        status = await client.get_status()
    finally:
        await client.close()
    identity = status.get("identity") or {}
    product = identity.get("product") if isinstance(identity, dict) else None
    return Validated(server_id=info.server_id, key_hex=key.hex(), product=product)


class NibeServiceUsbConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for nibe-service-usb."""

    VERSION = 1

    def __init__(self) -> None:
        self._host: str | None = None
        self._port: int = DEFAULT_PORT_VALUE
        self._name: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> NibeServiceUsbOptionsFlow:
        return NibeServiceUsbOptionsFlow()

    # ------------------------------------------------------------ helpers

    async def _try_validate(
        self, host: str, port: int, key_text: str
    ) -> tuple[Validated | None, dict[str, str]]:
        try:
            return await async_validate(host, port, key_text), {}
        except ValueError:
            return None, {CONF_KEY: "invalid_key"}
        except AuthenticationError:
            return None, {"base": "invalid_auth"}
        except ProtocolError:
            _LOGGER.warning("Gateway at %s:%s speaks an unsupported protocol", host, port)
            return None, {"base": "unsupported_proto"}
        except NibeError as err:
            _LOGGER.debug("Cannot connect to %s:%s: %s", host, port, err)
            return None, {"base": "cannot_connect"}
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error validating %s:%s", host, port)
            return None, {"base": "unknown"}

    def _title(self, validated: Validated, host: str) -> str:
        if self._name:
            return self._name
        if validated.product:
            return f"Nibe {validated.product}"
        return f"Nibe gateway ({host})"

    # ------------------------------------------------------------ manual

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input[CONF_PORT])
            validated, errors = await self._try_validate(host, port, user_input[CONF_KEY])
            if validated is not None:
                await self.async_set_unique_id(validated.server_id)
                self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})
                return self.async_create_entry(
                    title=self._title(validated, host),
                    data={
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_KEY: validated.key_hex,
                        CONF_SERVER_ID: validated.server_id,
                    },
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    # ------------------------------------------------------------ zeroconf

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        props = discovery_info.properties
        server_id = props.get("id")
        if not server_id:
            return self.async_abort(reason="no_server_id")
        proto = props.get("proto")
        if proto is not None and str(proto) != str(PROTO):
            return self.async_abort(reason="unsupported_proto")
        host = discovery_info.host
        port = discovery_info.port or DEFAULT_PORT_VALUE
        await self.async_set_unique_id(server_id)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})
        self._host = host
        self._port = port
        self._name = props.get("name") or discovery_info.name.split(".")[0]
        self.context["title_placeholders"] = {"name": self._name, "host": host}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._host is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            validated, errors = await self._try_validate(self._host, self._port, user_input[CONF_KEY])
            if validated is not None:
                if validated.server_id != self.unique_id:
                    return self.async_abort(reason="server_id_mismatch")
                return self.async_create_entry(
                    title=self._title(validated, self._host),
                    data={
                        CONF_HOST: self._host,
                        CONF_PORT: self._port,
                        CONF_KEY: validated.key_hex,
                        CONF_SERVER_ID: validated.server_id,
                    },
                )
        return self.async_show_form(
            step_id="zeroconf_confirm",
            data_schema=KEY_SCHEMA,
            errors=errors,
            description_placeholders={"name": self._name or "", "host": self._host, "port": str(self._port)},
        )

    # ------------------------------------------------------------ reauth

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host, port = entry.data[CONF_HOST], entry.data[CONF_PORT]
            validated, errors = await self._try_validate(host, port, user_input[CONF_KEY])
            if validated is not None:
                if validated.server_id != entry.unique_id:
                    return self.async_abort(reason="server_id_mismatch")
                return self.async_update_reload_and_abort(entry, data_updates={CONF_KEY: validated.key_hex})
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=KEY_SCHEMA,
            errors=errors,
            description_placeholders={"host": entry.data[CONF_HOST]},
        )

    # ------------------------------------------------------------ reconfigure

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = int(user_input[CONF_PORT])
            key_text = (user_input.get(CONF_KEY) or "").strip() or entry.data[CONF_KEY]
            validated, errors = await self._try_validate(host, port, key_text)
            if validated is not None:
                if validated.server_id != entry.unique_id:
                    return self.async_abort(reason="server_id_mismatch")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_HOST: host, CONF_PORT: port, CONF_KEY: validated.key_hex},
                )
        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_PORT): vol.All(vol.Coerce(int), vol.Range(1, 65535)),
                vol.Optional(CONF_KEY, default=""): str,
            }
        )
        suggested = user_input or {CONF_HOST: entry.data[CONF_HOST], CONF_PORT: entry.data[CONF_PORT]}
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )


class NibeServiceUsbOptionsFlow(OptionsFlow):
    """Whether to look for the gateway by mDNS after its address changed."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(CONF_RELOCATE_MDNS, default=options.get(CONF_RELOCATE_MDNS, True)): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
