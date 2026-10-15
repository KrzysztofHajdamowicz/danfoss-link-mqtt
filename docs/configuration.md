# Configuration

All settings are environment variables. Only `MQTT_HOST` is required. Empty optional values use defaults.

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
| `DANFOSS_ALLOW_WRITES` | true | false omits setpoint command topics and disables commands |
| `DANFOSS_STATE_VOLUME` | danfoss-link-mqtt-state | Compose-only Docker volume name |

The bridge exposes no inbound HTTP ports. It makes outgoing MQTT and authenticated cloud connections. Restart the container after changing environment settings. If another Compose deploy configuration already specifies memory limits, keep them consistent.

## MQTT entities and topics

The stable controller identifier is derived from the paired CC certificate's Subject Key Identifier. Room unique IDs use actual API room IDs, not display order or names. Renaming a room does not create a new climate entity.

Data live below `danfoss_link/<controller-id>/`:

| Topic | Payload |
| --- | --- |
| `availability` | online/offline |
| `room/<id>/state` | JSON temperature, setpoint, mode and action |
| `room/<id>/temperature/set` | Non-retained numeric temperature command |
| `system/state` | JSON current_state |
| `alarms/state` | JSON count |
| `alarms/attributes` | JSON alarms list |

Climate mode remains `heat`; `hvac_action` distinguishes heating from idle. It reports the controller's state, not measured valve position or hot-water flow. `ForecastHeating`, unknown and missing regulation states reset the action to unknown.

The controller sensors are **Systemstatus**, **Alarm**, and **Antal alarmer**. Alarm attributes preserve `id`, `isRead`, `createdDate`, `level`, `room`, `node`, `titleTextId`, and `descriptionTextId` when present. Text IDs are not currently translated. No phone notification automation is created, and alarms are never acknowledged or marked read by this client.

A successful empty list clears old alarm details. A missing/malformed alarm list causes an unavailable state, not a false zero count. Controller sensors also expire after max(180 seconds, three polling intervals).

Commands are QoS 0 with a clean MQTT session. Retained replay and MQTT DUP commands are rejected. Pending local commands expire after 30 seconds and are invalidated on disconnect. MQTT 3.1.1 does not preserve the publisher's retain flag on every live delivery; all other command publishers must also use `retain=false`. Uncertain PATCH operations are never retried automatically.
