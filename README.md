# Smart-G4 (S-BUS) for Home Assistant

Native Home Assistant integration for the
[Smart-G4 / S-BUS](https://www.smarthomebus.com/smart-bus-sbus-technology.html)
smart home system — an HDL-Buspro-style bus using the `SMARTCLOUD` UDP
signature. Local push, no cloud, no vendor software.

## Features

- **Config-flow setup with built-in bus discovery** — enter (or keep) the
  broadcast gateway address; the integration scans the bus and creates every
  module as a device, named with its on-bus name.
- **Relay / dimmer channels as switch entities** — state pushed live from the
  modules' own status broadcasts, plus polling for modules that support
  channel reads. Use HA's *"show as light"* helper to re-type channels that
  feed lights.
- **`smartg4_command` events** — every bus command (wall-panel press, scene,
  sequence, universal switch, curtain, panel control) is fired on the HA event
  bus with parsed data, so automations can react to physical panels directly.
- Discovery is collision-tolerant: S-BUS broadcast replies collide on the
  RS-485 side, so the scan bursts and accumulates until the inventory is
  complete.

## Requirements

- An RSIP / Z-Audio (or similar) gateway bridging the bus to your LAN,
  reachable by UDP broadcast on port 6000.
- Home Assistant must be on the same LAN subnet as the gateway.
- Home Assistant 2024.4 or newer.

## Installation (HACS)

1. HACS → ⋮ → **Custom repositories** → add this repository's URL, category
   **Integration**.
2. Install **Smart-G4 (S-BUS)** and restart Home Assistant.
3. *Settings → Devices & services → Add integration → Smart-G4 (S-BUS)*,
   keep the broadcast default (or your gateway's subnet broadcast, e.g.
   `192.168.1.255`) and let the bus scan finish.

## Supported modules

| Type code | Module | Entities |
|-----------|--------|----------|
| `0x01B8` | 12-channel relay | 12 switches (polled + push) |
| `0x07D3` | 3-channel relay/dimmer | 3 switches (push via status broadcast) |
| `0x0095` | DDP panel | events only (for now) |
| others | discovered & registered | events only (for now) |

Climate (HVAC), covers (curtains), media players (Z-Audio) and DDP button
programming are on the roadmap.

## Events for automations

Listen to `smartg4_command` in *Developer tools → Events*. Example trigger —
react to a wall-panel scene press:

```yaml
trigger:
  - platform: event
    event_type: smartg4_command
    event_data:
      command: scene
      source: "1.20"
```

## Credits

Protocol reverse-engineered from the official SDK and validated live on a
37-device installation; cross-checked against
[caligo-mentis/smart-bus](https://github.com/caligo-mentis/smart-bus) and
[wirenboard/wb-mqtt-smartbus](https://github.com/wirenboard/wb-mqtt-smartbus).
