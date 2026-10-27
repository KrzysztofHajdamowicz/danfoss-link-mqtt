# Configuration

All settings are environment variables. Only `MQTT_HOST` is required. Empty optional values use defaults. The Home Assistant add-on sets them from its options (see [the add-on documentation](../danfoss_link_cc_mqtt/DOCS.md)).

| Variable | Default | Meaning |
| --- | --- | --- |
| `MQTT_HOST` | required | Broker hostname or IP |
| `MQTT_PORT` | 1883; 8883 with TLS | Broker port |
| `MQTT_USERNAME` | empty | Optional broker username |
| `MQTT_PASSWORD` | empty | Optional password; requires username |
| `MQTT_TLS` | false | Set true for certificate-validated TLS |
| `MQTT_CA_FILE` | system CA store | Optional CA path inside the container; mount your CA file separately |
| `MQTT_TOPIC_PREFIX` | danfoss_link | Data/command prefix |
| `MQTT_DISCOVERY_PREFIX` | homeassistant | Home Assistant discovery prefix |
| `DANFOSS_POLL_INTERVAL` | 60 | Poll interval, 30–3600 seconds |
| `DANFOSS_ALLOW_WRITES` | true | false: read-only entities, no command topics, commands ignored |
| `DANFOSS_TIMEZONE` | `TZ`, else UTC | IANA time zone of the CC clock; holiday times are converted with it |
| `DANFOSS_SUGGEST_AREAS` | true | Suggest a Home Assistant area per room when its device is first added |
| `DANFOSS_PAIRING_CODE_FILE` | empty | File with a fresh cloud pairing code, used at startup while unpaired (see [pairing](pairing.md)) |
| `DANFOSS_STATE_VOLUME` | danfoss-link-mqtt-state | Compose-only Docker volume name |

The bridge exposes no inbound HTTP ports. It makes outgoing MQTT and authenticated cloud connections. Restart the container after changing environment settings. If another Compose deploy configuration already specifies memory limits, keep them consistent.

## Bridges

The image starts `hub_bridge.py`: a hub device with per-room devices, announced with MQTT **device discovery** (one `homeassistant/device/<id>/config` message per device). The original flat bridge, `mqtt_bridge.py` (one climate entity per room plus three controller sensors), is still included; select it with `command: python mqtt_bridge.py --state /state/apk-profile` in Compose. Run only one of them per paired identity.

When the hub bridge finds retained discovery from the flat bridge, it migrates those entities as Home Assistant documents it: `{"migrate_discovery": true}` to each old topic, then the device configuration, then an empty retained message on the old topic. Unique IDs are unchanged, so entity IDs and their settings survive. The hub's component keys deliberately differ from the flat bridge's object IDs, so Home Assistant does not see the final cleanup as a conflicting discovery message.

## Devices and entities

The stable controller identifier `<cid>` is derived from the paired CC certificate's Subject Key Identifier. Room unique IDs use actual API room IDs, not display order or names; renaming a room does not create new entities.

**Danfoss Link CC** (`danfoss_link_<cid>`):

| Entity | Platform | Source | Command |
| --- | --- | --- | --- |
| System status | sensor | `systemStatus.currentState` | — |
| Alarm, Alarm count | binary_sensor, sensor | `allUnresolvedAlarms` | — |
| Pause, Holiday, At home | switch | `modes[].state` | `PUT /api/modes/{pause,vacation,athome}` |
| Holiday start, Holiday end | datetime | active holiday times, otherwise the pending window | stored; sent when Holiday is switched on |
| Pause temperature, Holiday temperature | number | `temperatureSettings` | `PATCH /api/settings/temperature` |
| Last command | sensor (diagnostic) | result of the latest command | — |
| On/off devices | switch | `onOffDevices` | `PATCH /api/onoffdevices/<id>` |

**Danfoss Link – <room>** (`danfoss_link_<cid>_room_<id>`, via the hub):

| Entity | Platform | Source | Command |
| --- | --- | --- | --- |
| Thermostat | climate | `ambientTemperature`, `setpoint.temperature`, `regulationState`, `regulation.type` | `PATCH /api/rooms/<id>` setpoint |
| Temperature | sensor | `ambientTemperature` | — |
| At home setpoint, Away setpoint | number | `regulation.atHomeSetpoint` / `awaySetpoint` | `PATCH /api/rooms/<id>` regulation |

With `DANFOSS_ALLOW_WRITES=false`, switches become binary sensors, numbers and datetimes become sensors, and no command topics are announced.

Climate modes are `heat` (CC `Manual`) and `auto` (CC `Schedule`). They are shown but cannot be commanded, because the CC governs regulation per zone. `hvac_action` is `heating`, `idle` (`Deadband`) or `cooling`; `ForecastHeating`, unknown and missing states reset it to unknown. It reports the controller's state, not measured valve position or hot-water flow.

## Topics

Data live below `danfoss_link/<cid>/` and are retained:

| Topic | Payload |
| --- | --- |
| `availability` | online/offline |
| `system/state` | JSON `current_state` |
| `alarms/state`, `alarms/attributes` | JSON count; JSON alarms list |
| `hub/state` | JSON switches (`ON`/`OFF`), holiday window (ISO 8601 with offset), pause/holiday temperatures |
| `hub/last_command` | JSON `result`, `command`, `message`, `time` |
| `room/<id>/state` | JSON temperature, setpoint, mode, action, athome_setpoint, away_setpoint |
| `room/<id>/attributes` | JSON zone, regulation, room state, sensor presence, manual setpoint, schedule |
| `onoff/<id>/state` | ON/OFF |

Commands are non-retained QoS 0 messages:

| Topic | Payload |
| --- | --- |
| `room/<id>/temperature/set`, `room/<id>/athome_setpoint/set`, `room/<id>/away_setpoint/set` | number, 6–35 and within the room's limits |
| `hub/pause/set`, `hub/vacation/set`, `hub/athome/set`, `onoff/<id>/set` | `ON` / `OFF` |
| `hub/vacation_start/set`, `hub/vacation_end/set` | ISO 8601 with time zone |
| `hub/pause_temperature/set`, `hub/vacation_temperature/set` | number, 4–10 / 5–35 and within the CC's limits |

Commands use a clean MQTT session. Retained replay and MQTT DUP commands are rejected. Pending local commands expire after 30 seconds and are invalidated on disconnect. Bursts are coalesced: only the newest value per entity is written. MQTT 3.1.1 does not preserve the publisher's retain flag on every live delivery; all other command publishers must also use `retain=false`. Uncertain writes are never retried automatically.

Alarm attributes preserve `id`, `isRead`, `createdDate`, `level`, `room`, `node`, `titleTextId`, and `descriptionTextId` when present. Text IDs are not currently translated. Alarms are never acknowledged or marked read by this client. A successful empty list clears old alarm details; a missing or malformed alarm list causes an unavailable state, not a false zero count. Sensors expire after max(180 seconds, three polling intervals).
