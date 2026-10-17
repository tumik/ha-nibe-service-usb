"""Connection manager: keeps one authenticated connection to a gateway alive.

Owns the client, the sensor snapshot and the status copy, re-connects with
backoff, sends the heartbeat and re-sends the user's heating request after
every (re)connect (the server deliberately forgets it on restart or after a
watchdog revert, see the server's ``docs/security.md``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.storage import Store

from .client import (
    AuthenticationError,
    CommandError,
    ConnectionFailed,
    ConnectionLost,
    NibeError,
    NibeServiceUsbClient,
    ServerInfo,
)
from .const import (
    CONF_KEY,
    CONF_RELOCATE_MDNS,
    DOMAIN,
    PING_INTERVAL,
    RECONNECT_MAX,
    RECONNECT_MIN,
    RELOCATE_AFTER,
    RELOCATE_INTERVAL,
    STORAGE_VERSION,
)
from .discovery import find_by_mdns

_LOGGER = logging.getLogger(__name__)

STATUS_KEY = "__status__"
CLIENT_NAME = f"home-assistant/{HA_VERSION}"

type NibeConfigEntry = ConfigEntry[NibeGateway]


def client_name() -> str:
    return CLIENT_NAME


class NibeGateway:
    """State and connection for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.host: str = entry.data[CONF_HOST]
        self.port: int = entry.data[CONF_PORT]
        self._key: bytes = bytes.fromhex(entry.data[CONF_KEY])
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")

        self.client: NibeServiceUsbClient | None = None
        self.server: ServerInfo | None = None
        self.connected = False
        self.poll_interval: int | None = None
        self.sensors: dict[str, dict[str, Any]] = {}  # name -> metadata (without value/ts)
        self.values: dict[str, Any] = {}  # name -> last value
        self.status: dict[str, Any] = {}
        self.heating_request: str | None = None  # last mode the user chose; re-sent on reconnect

        self._listeners: dict[str, list[Callable[[], None]]] = {}
        self._new_sensor_listeners: list[Callable[[list[str]], None]] = []
        self._task: asyncio.Task[None] | None = None
        self._disconnected = asyncio.Event()
        self._stopping = False
        self._auth_failed_reported = False
        self._failing_since: float | None = None  # loop time of the first failure of the current streak
        self._relocate_lock = asyncio.Lock()

    # ------------------------------------------------------------ lifecycle

    async def async_start(self) -> None:
        """Load persisted state and make the first connection (raises for HA setup)."""
        stored = await self._store.async_load()
        if stored and stored.get("heating_request") in ("auto", "enable", "disable"):
            self.heating_request = stored["heating_request"]
        try:
            try:
                await self._connect_once()
            except ConnectionFailed:
                # HA may have restarted while the gateway sits on a new address.
                if not await self._async_relocate():
                    raise
                await self._connect_once()
        except AuthenticationError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except NibeError as err:
            raise ConfigEntryNotReady(f"Cannot connect to {self.host}:{self.port}: {err}") from err
        self._task = self.entry.async_create_background_task(
            self.hass, self._run(), name=f"{DOMAIN} connection {self.host}:{self.port}"
        )

    async def async_stop(self) -> None:
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self.client is not None:
            await self.client.close()
            self.client = None
        self._set_connected(False)

    async def async_remove_storage(self) -> None:
        await self._store.async_remove()

    # ------------------------------------------------------------ listeners

    @callback
    def async_add_listener(
        self, names: Iterable[str] | None, update: Callable[[], None]
    ) -> Callable[[], None]:
        """Call ``update`` when any of ``names`` changes (``None`` = status / connection only).

        Connection state changes always notify every listener.
        """
        keys = [STATUS_KEY] if names is None else list(names)
        for key in keys:
            self._listeners.setdefault(key, []).append(update)

        def remove() -> None:
            for key in keys:
                listeners = self._listeners.get(key)
                if listeners and update in listeners:
                    listeners.remove(update)

        return remove

    @callback
    def async_add_new_sensor_listener(self, add: Callable[[list[str]], None]) -> Callable[[], None]:
        """Platforms register here to be told when a later snapshot carries new sensors."""
        self._new_sensor_listeners.append(add)

        def remove() -> None:
            if add in self._new_sensor_listeners:
                self._new_sensor_listeners.remove(add)

        return remove

    def _notify(self, names: Iterable[str] | None) -> None:
        if names is None:
            targets = {cb for cbs in self._listeners.values() for cb in cbs}
        else:
            targets = {cb for name in names for cb in self._listeners.get(name, ())}
        for cb in targets:
            try:
                cb()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Listener failed")

    # ------------------------------------------------------------ connection

    def _set_connected(self, connected: bool) -> None:
        if self.connected != connected:
            self.connected = connected
            self._notify(None)

    async def _connect_once(self) -> None:
        """Connect, authenticate, subscribe, fetch status and re-send the heating request."""
        client = NibeServiceUsbClient(
            self.host,
            self.port,
            self._key,
            client_name=CLIENT_NAME,
            on_event=self._on_event,
            on_disconnect=self._on_disconnect,
        )
        server = await client.connect()
        expected_id = self.entry.data.get("server_id") or self.entry.unique_id
        if expected_id and server.server_id != expected_id:
            # Another gateway took over this address: do not talk to (or control) it.
            await client.close()
            raise ConnectionFailed(
                f"{self.host}:{self.port} is gateway {server.server_id}, expected {expected_id}"
            )
        self.server = server
        # From here on a drop is reported through _on_disconnect; arm it before any further await.
        self.client = client
        self._disconnected.clear()
        try:
            sub = await client.subscribe(["*"])
            status = await client.get_status()
        except NibeError:
            await client.close()
            raise
        self._auth_failed_reported = False
        self._failing_since = None
        self.poll_interval = sub.get("poll_interval")
        self._apply_snapshot(sub.get("sensors") or {})
        self.status = status
        if self.heating_request is not None:
            try:
                result = await client.set_heating_request(self.heating_request)
                self._apply_heating_result(result)
            except CommandError as err:
                _LOGGER.warning("Could not re-send heating request %s: %s", self.heating_request, err)
            except ConnectionLost as err:
                _LOGGER.warning("Connection lost while re-sending heating request: %s", err)
        if not client.connected:
            raise ConnectionLost("connection dropped during setup")
        self._set_connected(True)
        self._notify(None)
        _LOGGER.info(
            "Connected to %s (server %s, %d sensors)", self.host, self.server.server_id, len(self.sensors)
        )

    def _apply_snapshot(self, sensors: dict[str, dict[str, Any]]) -> None:
        new: list[str] = []
        for name, entry in sensors.items():
            if not isinstance(entry, dict):
                continue
            meta = {k: v for k, v in entry.items() if k not in ("value", "ts", "failures")}
            if name not in self.sensors:
                new.append(name)
            self.sensors[name] = meta
            self.values[name] = entry.get("value")
        if new and self._new_sensor_listeners:
            for add in list(self._new_sensor_listeners):
                add(new)
        self._notify(list(sensors))

    def _apply_heating_result(self, result: dict[str, Any]) -> None:
        heating = dict(self.status.get("heating") or {})
        for key in ("mode", "active", "suspended_reason", "current_dm"):
            if key in result:
                heating[key] = result[key]
        self.status["heating"] = heating
        self._notify([STATUS_KEY])

    def _option(self, key: str) -> bool:
        return bool(self.entry.options.get(key, True))

    async def _async_relocate(self) -> bool:
        """Look for the gateway by its server id; on success store and use the new address.

        Uses an mDNS lookup by TXT id (switchable in the options). At most one attempt per
        ``RELOCATE_INTERVAL`` (also across the entry's setup retries).
        """
        server_id = self.entry.unique_id or self.entry.data.get("server_id")
        if not server_id or not self._option(CONF_RELOCATE_MDNS):
            return False
        async with self._relocate_lock:
            if self._stopping:
                return False
            loop = asyncio.get_running_loop()
            attempts: dict[str, float] = self.hass.data.setdefault(f"{DOMAIN}_relocate", {})
            last = attempts.get(self.entry.entry_id)
            if last is not None and loop.time() - last < RELOCATE_INTERVAL:
                return False
            attempts[self.entry.entry_id] = loop.time()

            found = await find_by_mdns(self.hass, server_id)
            if found is None:
                _LOGGER.debug("Gateway %s not found by mDNS", server_id)
                return False
            host, port = found
            if (host, port) == (self.host, self.port):
                return False
            _LOGGER.info("Gateway %s found at %s:%s (was %s:%s)", server_id, host, port, self.host, self.port)
            self.hass.config_entries.async_update_entry(
                self.entry, data={**self.entry.data, CONF_HOST: host, CONF_PORT: port}
            )
            self.host, self.port = host, port
            self._failing_since = None
            return True

    async def _run(self) -> None:
        """Background loop: ping while connected, reconnect with backoff when not."""
        backoff = RECONNECT_MIN
        while not self._stopping:
            if not self.connected:
                try:
                    await self._connect_once()
                    backoff = RECONNECT_MIN
                except AuthenticationError as err:
                    if not self._auth_failed_reported:
                        _LOGGER.error("Authentication with %s failed: %s", self.host, err)
                        self._auth_failed_reported = True
                        self.entry.async_start_reauth(self.hass)
                    backoff = RECONNECT_MAX
                    self._failing_since = None
                except (NibeError, OSError) as err:
                    _LOGGER.log(
                        logging.WARNING if backoff == RECONNECT_MIN else logging.DEBUG,
                        "Connection to %s:%s failed (%s); retrying in %.0f s",
                        self.host,
                        self.port,
                        err,
                        backoff,
                    )
                    now = asyncio.get_running_loop().time()
                    if self._failing_since is None:
                        self._failing_since = now
                    if now - self._failing_since >= RELOCATE_AFTER and await self._async_relocate():
                        backoff = RECONNECT_MIN
                        continue
                if not self.connected:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, RECONNECT_MAX)
                    continue
            client = self.client
            assert client is not None
            try:
                await asyncio.wait_for(self._disconnected.wait(), PING_INTERVAL)
                self._set_connected(False)
                continue  # dropped; loop reconnects
            except TimeoutError:
                pass
            try:
                await client.ping()
            except NibeError as err:
                _LOGGER.warning("Heartbeat to %s failed: %s", self.host, err)
                await client.close()

    def _on_disconnect(self, reason: Exception | None) -> None:
        if self._stopping:
            _LOGGER.debug("Connection to %s:%s closed on unload", self.host, self.port)
        else:
            _LOGGER.warning("Connection to %s:%s closed (%s)", self.host, self.port, reason or "by server")
        self._disconnected.set()
        self._set_connected(False)

    def _on_event(self, name: str, data: dict[str, Any]) -> None:
        if name == "sensor_update":
            values = data.get("values")
            if isinstance(values, dict):
                changed = [n for n, v in values.items() if n in self.sensors]
                for n in changed:
                    self.values[n] = values[n]
                self._notify(changed)
        elif name == "status":
            was_online = self.pump_online
            self.status = data
            if self.pump_online != was_online:
                # Per-sensor entities derive availability from pump.online.
                self._notify(None)
            else:
                self._notify([STATUS_KEY])
        elif name == "shutdown":
            _LOGGER.info("Gateway %s is shutting down (%s)", self.host, data.get("reason"))
        else:
            _LOGGER.debug("Unhandled event %s: %s", name, data)

    # ------------------------------------------------------------ commands

    def _require_client(self) -> NibeServiceUsbClient:
        if self.client is None or not self.connected:
            raise ConnectionLost("gateway is not connected")
        return self.client

    async def async_set_hot_water_mode(self, mode: str) -> dict[str, Any]:
        result = await self._require_client().set_hot_water_mode(mode)
        if "hw_comfort" in self.sensors and result.get("mode"):
            self.values["hw_comfort"] = result["mode"]
            self._notify(["hw_comfort"])
        hot_water = dict(self.status.get("hot_water") or {})
        hot_water["mode"] = result.get("mode", mode)
        self.status["hot_water"] = hot_water
        self._notify([STATUS_KEY])
        return result

    async def async_set_heating_request(self, mode: str) -> dict[str, Any]:
        """Send the request and remember it for every later reconnect.

        A mode the server rejects (``CommandError``) is not remembered; one that
        could not be delivered because the link is down is, so it goes out as
        soon as the gateway is back.
        """
        try:
            result = await self._require_client().set_heating_request(mode)
        except ConnectionLost:
            await self._remember_heating_request(mode)
            raise
        await self._remember_heating_request(mode)
        self._apply_heating_result(result)
        return result

    async def _remember_heating_request(self, mode: str) -> None:
        if self.heating_request != mode:
            self.heating_request = mode
            await self._store.async_save({"heating_request": mode})

    async def async_trigger_hot_water_boost(self) -> dict[str, Any]:
        return await self._require_client().trigger_hot_water_boost()

    async def async_refresh(self) -> dict[str, Any]:
        return await self._require_client().refresh()

    # ------------------------------------------------------------ helpers

    @property
    def pump_online(self) -> bool:
        pump = self.status.get("pump") or {}
        return bool(pump.get("online"))

    @property
    def identity(self) -> dict[str, Any]:
        return self.status.get("identity") or {}

    @property
    def server_id(self) -> str:
        if self.server is not None:
            return self.server.server_id
        return str(self.entry.unique_id)
