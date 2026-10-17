# Nibe service USB gateway for Home Assistant

[![Validate](https://github.com/tumik/ha-nibe-service-usb/actions/workflows/validate.yml/badge.svg)](https://github.com/tumik/ha-nibe-service-usb/actions/workflows/validate.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)

**Remote monitoring and control for Nibe heat pumps that have no official way to do it.** Some
Nibe models, such as the **F1226**, have no network interface, no Modbus interface, and no Modbus
or internet add-on you can buy for them. Together with its gateway, this integration connects them
to **Home Assistant** anyway, through the pump's **service USB port**.

**No heat pump add-ons needed: just a USB cable and a small Linux computer** next to the pump (a
Raspberry Pi is enough). Home Assistant gets the pump's operating data (all temperatures,
compressor and pump state, degree minutes, hot water mode, alarms, counters and settings) as
sensors, and can control the pump: hot water comfort mode (eco / normal / lux), an immediate
periodic hot water increase (anti-legionella), and holding space heating on or off. Everything
stays local: no cloud account, no Modbus module, no changes to the pump's wiring, and no USB access
needed on the Home Assistant machine.

> **Install the gateway first.** This integration does not talk to the pump itself. It connects
> to **[nibe-service-usb](https://github.com/tumik/nibe-service-usb)**, the gateway server that
> runs on the computer with the USB cable to the heat pump's service port.
>
> **Read the gateway's [safety warning](https://github.com/tumik/nibe-service-usb#nibe-service-usb)
> before you connect anything.** It has been tested on one unit only (a Nibe F1226-6 R), the
> service port is a service-only interface, and some settings (such as the hot water comfort mode)
> are stored in the pump's EEPROM, which wears out: avoid automations that change them many times
> a day.

## How it fits together

```
 Nibe heat pump
      |  service USB port (serial console)
      v
 gateway host  --  nibe-service-usb  (fixed command allowlist, key-based auth)
      |  TCP 9477 on your LAN  (+ mDNS announcement _nibe-usb._tcp)
      v
 Home Assistant  --  this integration
```

## Requirements

- Home Assistant **2025.3** or newer (see [Compatibility](#compatibility)).
- A running [nibe-service-usb](https://github.com/tumik/nibe-service-usb) gateway and its key.
- Network: Home Assistant must reach the gateway on TCP port 9477 (the default). Auto-discovery
  needs mDNS between the two (same LAN/VLAN, or an mDNS reflector); without it, add the gateway by
  address.
- [HACS](https://hacs.xyz) is optional; a manual copy works too.

## Quick start

1. **Install the gateway** on the host that has the service USB cable. Follow the
   [Install section of its README](https://github.com/tumik/nibe-service-usb#install) and start it
   (`nibe-service-usb run`, or the systemd unit it ships).
2. **Get the key.** On the gateway host run `nibe-service-usb show-key`. Keep the output; you will
   paste it into Home Assistant.
3. **Install the integration.**
   - *HACS:* three-dot menu, **Custom repositories**, add
     `https://github.com/tumik/ha-nibe-service-usb` with category **Integration**, then install
     "Nibe service USB gateway".
   - *Manual:* copy `custom_components/nibe_service_usb` from this repository to
     `<config>/custom_components/nibe_service_usb`.
4. **Restart Home Assistant.**
5. **Add the integration.** If mDNS works, a discovered "Nibe service USB gateway" card appears
   under **Settings, Devices & services**; click **Add**. Otherwise choose **Add integration**,
   search for "Nibe service USB gateway", and enter host, port (9477) and key.
6. **Paste the key** (64 hex characters or the dash-grouped base32 form; both are accepted) and
   submit. The device and its entities appear within a few seconds.

The key is never sent over the network. The connection uses an HMAC-SHA256 challenge/response in
both directions, so the integration also verifies that the other end knows the key.

## Configuration

Everything is set up in the UI; there is no YAML.

| Where | What |
|---|---|
| **Configure** (options) | *Search with mDNS* (default on): look for the gateway by its server id when it stops answering at the stored address (see [Address changes](#address-changes)). |
| **Reconfigure** (three-dot menu of the integration) | Change host, port and optionally the key by hand. Leave the key empty to keep the stored one. The gateway at the new address must have the same server id, otherwise the change is refused. |
| **Re-authentication** | Starts by itself when the gateway rejects the key, for example after `nibe-service-usb rotate-key`. Enter the new key (`show-key`). |

### Address changes

A gateway is identified by its server id (not its IP address), so adding the same gateway twice
gives the same entry and a moved gateway is found again:

1. **mDNS rediscovery.** When Home Assistant sees the gateway's `_nibe-usb._tcp` announcement with
   the stored id at a new address, host and port are updated and the entry reloads.
2. **Lookup by id.** If the gateway has been unreachable for 90 s, or the first connection fails
   when Home Assistant starts, the integration sends one mDNS query (at most every 5 minutes) for
   the stored server id and reconnects at the address it finds. Nothing is sent to other
   addresses on your network. Turn this off under **Configure**.
3. **Reconfigure.** Enter the new address by hand (needed across subnets without an mDNS
   reflector).

A different gateway answering on the old address is refused: the server id must match.

## Entities

All entities belong to one device per gateway. Model, software version and product code come from
the pump identity the gateway reports. Entity ids start with the device name, for example
`select.nibe_heat_pump_hot_water_comfort` (the name is the gateway's `discovery.name`, or
"Nibe " plus the pump model if you added it manually).

### Controls

| Entity | Type | Purpose |
|---|---|---|
| Heating request | select: `auto`, `enable`, `disable` | Ask the pump to heat or to hold off. See [below](#what-the-heating-request-does). |
| Hot water comfort | select: `eco`, `normal`, `lux` | The pump's hot water comfort setting. Stored on the pump, so it persists there. |
| Start periodic hot water increase | button | Starts an anti-legionella cycle now. Rate limited to one per 10 minutes; takes about 5-20 s to answer. |
| Refresh sensors | button | Asks the gateway to re-read all sensors now. Rate limited to once per 60 s. |

### Sensors

The sensor list comes from the gateway, so the set follows its sensor definitions (new sensors are
added without a reload). Main ones:

| Group | Examples |
|---|---|
| Temperatures | outdoor (BT1) and average outdoor, flow (BT2), return (BT3), hot water charge and top (BT6, BT7), brine in/out (BT10, BT11), condenser out (BT12), hot gas (BT14), liquid line (BT15), suction gas (BT17), calculated flow |
| Heating control | degree minutes, operating priority, heat demand, electric addition power and step |
| Compressor and pumps | compressor running / state / status / inhibit time, heat medium pump (GP1) and brine pump (GP2) speed, hot water valve position |
| Hot water | effective hot water mode and reason, periodic increase running / remaining / hold / failed, temporary lux |
| Alarms | "Alarm" (binary, problem) and "Active alarms" (state = count, attribute `alarms` = list of number and title), freeze protection |
| Counters | compressor starts, compressor hours (total and hot water) |
| Pump settings | disabled by default, diagnostic category (start compressor threshold, stop heating, curve, ...) |

### Diagnostic entities

Gateway connected, pump online, watchdog alive, EEPROM write failed, pump identity (product,
software version, product code, clock state and drift, last restart and health check), gateway
version and uptime, connected clients, last heartbeat, watchdog reverted at, and the heating
request's target and current degree minutes (raw units).

Entities become unavailable while the gateway is unreachable. Sensors also become unavailable
while the pump is offline or a value is empty.

### What the heating request does

It does **not** switch the compressor on or off. The gateway only nudges the pump's
degree-minute counter and the pump's own control logic decides; the pump keeps full authority and
the heating curve, setpoints and operating mode are never changed.

- `auto`: nothing is written. The pump runs by itself.
- `enable`: every 10 minutes the counter is set to just above the compressor start threshold, so
  the compressor starts at the pump's next one-minute step if the pump considers heat to be
  needed. If the house is warmer than the pump's target, nothing starts. It is never written when
  it would reduce heating.
- `disable`: every 10 minutes the counter is reset to -1 (raw units). If the pump already thinks
  the house is warm enough, the compressor stops and cannot restart until the deficit builds up
  again from -1. If the house is colder than the target, heating starts normally.
- Nothing is written while the counter is exactly 0 (the pump itself is blocking heating), while
  hot water is being charged, or while the pump is offline.
- **Safety guards** suspend `disable` (the mode stays selected but nothing is written) when the
  supply temperature is below its limit (default 20 C), the outdoor temperature is below its
  limit (default -15 C), an optional room limit is violated, or sensor data is stale (default
  older than 900 s). Guards clear with 1 C hysteresis. `enable` has no guards. Limits are set on
  the gateway; see its
  [heating-control documentation](https://github.com/tumik/nibe-service-usb/blob/main/docs/heating-control.md).
- **Watchdog:** the integration pings the gateway every 60 s. If the gateway hears nothing from
  Home Assistant for 5 minutes, it reverts the request to `auto`. A connection drop alone does not
  revert it, so a Home Assistant restart does not flap the pump.
- **Persistence:** the gateway forgets the request on restart or after a watchdog revert, so the
  integration stores your last choice and re-sends it after every reconnect and every Home
  Assistant restart. Hot water comfort is not re-sent, because the pump stores it.

The select entity has the attributes `active`, `suspended_reason`, `target_dm`, `current_dm`,
`last_push` and `next_push`.

## Example automations

These are examples. Adjust entity ids (check **Settings, Devices & services, Entities**), the
price sensor and the thresholds to your setup.

Hot water comfort `normal` during the cheap night hours and `eco` during the day. Every comfort
change is one write to the pump's EEPROM, so keep changes to a few a day: this example switches
twice a day, and only when the mode actually differs.

```yaml
automation:
  - alias: "Hot water comfort: normal at night, eco by day"
    triggers:
      - trigger: time
        at: "22:00:00"
        id: night
      - trigger: time
        at: "06:00:00"
        id: day
    variables:
      target: "{{ 'normal' if trigger.id == 'night' else 'eco' }}"
    conditions:
      - condition: template
        value_template: "{{ states('select.nibe_heat_pump_hot_water_comfort') != target }}"
    actions:
      - action: select.select_option
        target:
          entity_id: select.nibe_heat_pump_hot_water_comfort
        data:
          option: "{{ target }}"
```

Hold off heating during the most expensive hours and return control to the pump afterwards
(the pump still heats if the house gets too cold; see the safety guards above):

```yaml
automation:
  - alias: "Request heating off when price is high"
    triggers:
      - trigger: numeric_state
        entity_id: sensor.electricity_price
        above: 0.30
    actions:
      - action: select.select_option
        target:
          entity_id: select.nibe_heat_pump_heating_request
        data:
          option: disable

  - alias: "Heating request back to auto"
    triggers:
      - trigger: numeric_state
        entity_id: sensor.electricity_price
        below: 0.30
    actions:
      - action: select.select_option
        target:
          entity_id: select.nibe_heat_pump_heating_request
        data:
          option: auto
```

Notify on an alarm:

```yaml
automation:
  - alias: "Heat pump alarm"
    triggers:
      - trigger: state
        entity_id: binary_sensor.nibe_heat_pump_alarm
        to: "on"
    actions:
      - action: notify.notify
        data:
          title: "Heat pump alarm"
          message: >-
            {{ state_attr('sensor.nibe_heat_pump_active_alarms', 'alarms')
               | map(attribute='title') | join(', ') }}
```

## Troubleshooting

| Problem | What to check |
|---|---|
| **Cannot connect** | Is the gateway running (`systemctl status`, or run it in a terminal)? Can Home Assistant's host reach the port (`nc -vz <host> 9477`)? Check firewalls and `listen_host` in the gateway config. |
| **Invalid key** | Compare with `nibe-service-usb show-key` on the gateway host. If the key was rotated, use the re-authentication prompt. After repeated failures the gateway throttles that IP address for a while. |
| **Not discovered** | mDNS does not cross VLANs or subnets without a reflector, and Home Assistant in a container needs host networking. Also check `discovery.enabled` on the gateway. Add the integration manually instead. |
| **"Server proof mismatch" / server id mismatch** | Another device answers on that address or port, or the gateway was reinstalled (new server id). Check host and port; for a reinstalled gateway, remove the entry and add it again. |
| **Unsupported protocol** | The gateway and the integration speak different protocol versions; update the older one. |
| **Entities unavailable** | "Gateway connected" off means Home Assistant cannot reach the gateway. "Pump online" off means the gateway cannot reach the pump (cable, USB, pump power). |
| **Gateway got a new IP** | Normally found again by mDNS within a few minutes. Otherwise use **Reconfigure**. |

Enable debug logging:

```yaml
logger:
  logs:
    custom_components.nibe_service_usb: debug
```

**Diagnostics:** **Settings, Devices & services**, open the integration, three-dot menu,
**Download diagnostics**. The key is redacted; host, port and server id are included, so look
through the file before posting it publicly.

## Development

```
python3 -m venv .venv
.venv/bin/pip install pytest-homeassistant-custom-component ruff
.venv/bin/python -m pytest -q tests
.venv/bin/ruff check custom_components tests
```

To try the integration without a pump, run the gateway's built-in simulator:

```
git clone https://github.com/tumik/nibe-service-usb && cd nibe-service-usb
uv sync && uv run nibe-service-usb simulate
```

It prints the key and the listening port. Add the integration manually with host `127.0.0.1`,
that port and key. See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow.

## Compatibility

The minimum is Home Assistant 2025.3, the release that added
`AddConfigEntryEntitiesCallback`, the newest API the integration relies on. Development and tests
run on a current Home Assistant release.

## Roadmap / To do

- [ ] **Force water heating with max power.** If running out of water, force both compressor and
  additional heating to heat water. This needs support in the gateway server first (a server-side
  feature, new command in its allowlist); the integration would then expose a button or service
  for it.

## Disclaimer

This is an independent project and is **not affiliated with or endorsed by NIBE**. Connecting to
the pump's service port and changing its settings is done **at your own risk**; check what it
means for your warranty and installation. The gateway exposes only a fixed allowlist of
high-level commands (hot water comfort, periodic increase, heating request, status); the raw pump
protocol is never exposed and the pump keeps its own safety logic.

## License

MIT, see [LICENSE](LICENSE).
