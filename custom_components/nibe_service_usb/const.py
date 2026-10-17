"""Constants for the nibe-service-usb integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "nibe_service_usb"
MANUFACTURER = "Nibe"
ZEROCONF_TYPE = "_nibe-usb._tcp.local."

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.SELECT,
    Platform.SENSOR,
]

CONF_KEY = "key"
CONF_SERVER_ID = "server_id"
CONF_RELOCATE_MDNS = "relocate_mdns"
DEFAULT_PORT_VALUE = 9477

PING_INTERVAL = 60.0  # the server watchdog reverts after 300 s without a request
RECONNECT_MIN = 1.0
RECONNECT_MAX = 60.0

# Looking for the gateway at a new address (see discovery.py): only after this long without a
# connection, and at most once per interval.
RELOCATE_AFTER = 90.0
RELOCATE_INTERVAL = 300.0

# Sensor tiers that become diagnostic entities, and those disabled by default.
DIAGNOSTIC_TIERS = frozenset({"settings", "identity", "diagnostic"})
DISABLED_TIERS = frozenset({"settings"})

STORAGE_VERSION = 1
