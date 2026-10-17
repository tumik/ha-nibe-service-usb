# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Every push to `main` that passes CI is released
automatically (see [CONTRIBUTING.md](CONTRIBUTING.md#releases)); GitHub release notes list the
merged changes, this file summarises the notable ones.

## [Unreleased]

## [0.2.0] - 2026-10-17

Initial public release.

### Added

- Config flow with zeroconf discovery (`_nibe-usb._tcp`) and manual host, port and key entry,
  with HMAC-SHA256 mutual authentication against the gateway; re-authentication for a rotated
  key; reconfigure flow to change host, port or key.
- Sensors and binary sensors created from the gateway's sensor list, including a list of active
  alarms; settings sensors are disabled by default, identity and diagnostic ones are in the
  diagnostic category.
- Select entities for hot water comfort and the heating request; the last heating request is
  stored and re-sent after every reconnect and Home Assistant restart.
- Buttons to start a periodic hot water increase and to refresh the sensors.
- Diagnostic sensors and binary sensors from the gateway status (connection, pump online,
  watchdog, EEPROM write failure, identity).
- Heartbeat every 60 s, reconnect with exponential backoff, and a lookup of the gateway's mDNS
  announcement by server id when it has been unreachable (option **Search with mDNS**); the
  integration refuses a gateway whose server id differs from the configured one.
- Diagnostics download with the key redacted.
- Brand icon.

[Unreleased]: https://github.com/tumik/ha-nibe-service-usb/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/tumik/ha-nibe-service-usb/releases/tag/v0.2.0
