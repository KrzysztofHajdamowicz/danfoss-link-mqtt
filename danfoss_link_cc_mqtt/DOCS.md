# Danfoss Link CC (MQTT)

Brings a Danfoss Link CC into Home Assistant through MQTT discovery. The add-on is an additional
paired client of the CC, using the same Danfoss cloud relay as the retired phone app. No CC firmware
changes and no custom integration are needed.

## Requirements

- Home Assistant 2026.5 or newer (holiday start/end use MQTT `datetime` entities).
- The MQTT integration with a broker. The Mosquitto broker add-on is found automatically.
- The CC must stay online to the Danfoss (Trifork) relay; the add-on talks to the CC only through it.

## Install

[![Add the repository to your Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FKrzysztofHajdamowicz%2Fdanfoss-link-mqtt)
[![Open the add-on in your Home Assistant](https://my.home-assistant.io/badges/supervisor_app.svg)](https://my.home-assistant.io/redirect/supervisor_app/?app=e7f0e068_danfoss_link_cc_mqtt&repository_url=https%3A%2F%2Fgithub.com%2FKrzysztofHajdamowicz%2Fdanfoss-link-mqtt)

1. Add the repository (first button), or manually add
   `https://github.com/KrzysztofHajdamowicz/danfoss-link-mqtt` under the add-on (app) store's
   **⋮ → Repositories**.
2. Install **Danfoss Link CC (MQTT)** (second button) and start it. Until it is paired, the log
   says `NOT PAIRED` and the add-on waits.

## Pair once

Use **one** of these:

- **Reuse an identity that is already paired**, for example from the standalone container. Copy
  `app_key.pem`, `app_cert.pem` and `paired_cc.json` into this add-on's configuration folder,
  `/addon_configs/<prefix>_danfoss_link_cc_mqtt/` (Samba, SSH or File editor add-on). From a Docker volume:

  ```sh
  docker run --rm -v danfoss-link-mqtt-state:/s -v "$PWD/out":/o alpine \
    cp /s/apk-profile/app_key.pem /s/apk-profile/app_cert.pem /s/apk-profile/paired_cc.json /o/
  ```

  The add-on imports them with private permissions within a few seconds. Delete the copies from the
  configuration folder afterwards, and stop the old container: two bridges must not share an identity.
- **Pair fresh**: on the CC panel, open the remote/app connection menu and generate a cloud code
  (codes starting with `0000` are local codes and are refused). From the Home Assistant host shell:

  ```sh
  docker exec -it $(docker ps -qf name=danfoss_link_cc_mqtt) \
    python /app/pair_cc.py --state /data --apk-identity-test --name "Home Assistant"
  ```

  Type the code at the hidden prompt. Never put the code in options, arguments or logs.

Pairing state lives in the add-on's `/data`, so Home Assistant backups of the add-on contain the
paired private key. Protect backups accordingly.

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `mqtt_host` | empty | Broker host; empty uses the Mosquitto add-on |
| `mqtt_port`, `mqtt_username`, `mqtt_password`, `mqtt_tls` | empty | Manual broker settings |
| `topic_prefix` | `danfoss_link` | Prefix for state and command topics |
| `discovery_prefix` | `homeassistant` | Home Assistant discovery prefix |
| `poll_interval` | 60 | Seconds between cloud reads (30–3600) |
| `allow_writes` | true | Off: read-only entities, no commands |
| `suggest_areas` | true | Suggest an area per CC room when its device is first added |
| `timezone` | Home Assistant's | IANA zone of the CC clock, for holiday times |

## Devices and entities

**Danfoss Link CC** (hub): system status, alarm and alarm count, switches for **Pause**, **Holiday**
and **At home**, **Holiday start**/**Holiday end**, **Pause temperature** and **Holiday temperature**,
and a diagnostic **Last command** sensor showing whether the latest command was applied, rejected by
the CC (with its message), unverified or expired.

**Danfoss Link – <room>** for every CC room, connected via the hub: a thermostat (current and target
temperature, heating/idle action), a temperature sensor for history and statistics, and
**At home setpoint** / **Away setpoint**. The schedule and zone are thermostat attributes.

## How commands behave

- Every write is preceded by a fresh read, sent once, and confirmed by reading the CC again. A
  write whose outcome is unknown is never retried automatically; the entities go unavailable until
  the next successful read.
- Rapid changes (dragging a slider) are combined: only the latest value per entity is sent.
- **Pause and Holiday block target-temperature changes.** The CC holds every room at frost
  protection; the add-on refuses such changes and reports this in **Last command**.
- Heat/auto (manual/schedule) is decided by the CC zone, so it is shown but cannot be changed.
- **Holiday:** set **Holiday end** (and optionally **Holiday start**) first, then switch
  **Holiday** on. A start in the past or left empty means "now". Times are interpreted in the CC's
  time zone. While Holiday is active, changing start or end updates the CC directly.
- Activating Holiday or At home has only been tested against a simulated CC so far; switching Pause on
  and off, setpoints and the pause/holiday temperatures were verified on a real CC.

## Moving from the standalone container

Stop the old container, reuse its identity (above) and start the add-on. Entities created by the
earlier flat model (per-room climate, status and alarm sensors) are migrated automatically to the new
devices, keeping their entity IDs and settings.
