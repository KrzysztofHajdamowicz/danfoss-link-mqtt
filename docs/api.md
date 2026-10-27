# Observed Danfoss Link cloud protocol

This document separates static observations from live verification. It is an interoperability record, not an official API specification. The app's version is 1.3.10; tested CC firmware is 4.2.1724 on MK 4.1.

## Transport

The client is an additional paired application. It does not call a public HTTPS REST base URL. It establishes a CA-validated TLS connection to the MDG grid, obtains an authenticated router referral, negotiates peer TLS 1.2 and sends framed `cc_json` messages to the CC through the cloud.

Observed grid addresses: `77.66.11.90`, `77.66.11.92`, `5.179.92.180`, `5.179.92.182`, TCP port 443. These are service addresses, not household/device addresses. The grid handshake magic is 4036857000; router handshake magic is 4036856981. The router token is transient and is not logged. Protocol schemas and field numbers are in `src/danfoss_mdg/schemas.json`.

Normal routing uses the paired certificate Subject Key Identifier, formatted as lowercase colon-separated hex, and protocol `cc_json`. Peer application messages use a two-byte big-endian length followed by UTF-8 JSON.

## JSON envelope

Read example:

```json
{"id":1,"data":{"type":"JsonWebRequest","data":{"method":"GET","uri":"/api/rooms","headers":["Accept: application/json; charset=utf-8"]}}}
```

Temperature write example (illustrative room ID):

```json
{"id":2,"data":{"type":"JsonWebRequest","data":{"method":"PATCH","uri":"/api/rooms/5","headers":["Accept: application/json; charset=utf-8"],"content":{"id":5,"setpoint":{"temperature":19.0}}}}}
```

The `JsonWebResponse` envelope contains the matching `id` and `data.data.status`, with optional `data.data.content`. Only HTTP-style status integers 200–299 are accepted. This is HTTP-style request data inside the peer protocol, not an ordinary HTTP connection to the CC. The original peer layer handles PATCH content headers.

## Read and write coverage

| Resource | Method | Evidence |
| --- | --- | --- |
| `/api/world` | GET | Static request/parser and live fields verified |
| `/api/rooms` | GET | Live room reads verified |
| `/api/rooms/<id>` | GET | Static request; not used by current MQTT poller |
| `/api/rooms/<id>` | PATCH | Live setpoint write and GET readback verified |
| `/api/system/info` | GET | Live read verified |
| `/api/system/apiversions` | GET | Static mapping; versions field observed inside world |
| `/api/alarms` | GET | Static request/parser; current poller reads alarms from world |
| `/api/system/status` | GET | Static request; status observed inside world |
| `/api/onoffdevices` | GET | Static request; empty list observed inside world |

The hub bridge (`src/hub_transport.py`) sends only requests that match an explicit allowlist of methods, URIs and exact `content` shapes; anything else is rejected before it leaves the process. Writes carry `Accept` and `Content-Type: application/json; charset=utf-8` headers.

| Operation | Method and URI | Content | Evidence |
| --- | --- | --- | --- |
| Room target temperature | `PATCH /api/rooms/<id>` | `{"id":<id>,"setpoint":{"temperature":T}}` | Live write and readback verified |
| Room at-home / away setpoint | `PATCH /api/rooms/<id>` | `{"id":<id>,"regulation":{"atHomeSetpoint":T}}` or `awaySetpoint` | Live status 200 |
| Pause on/off | `PUT /api/modes/pause` | `{"type":"Pause","state":"Active"\|"Inactive"}` | Live, both directions |
| Holiday on/off | `PUT /api/modes/vacation` | `{"type":"Vacation","state":…}`, plus `startTime`/`endTime` (CC-local `YYYY-MM-DDTHH:MM:SS`) when activating | Live for `Inactive`; activation synthetic only |
| At home on/off | `PUT /api/modes/athome` | `{"type":"AtHome","state":…}` | Live for `Inactive`; activation synthetic only |
| Pause / holiday temperature | `PATCH /api/settings/temperature` | `{"pauseTemperature":{"setpoint":T}}` or `vacationTemperature` | Live status 200 |
| On/off device | `PATCH /api/onoffdevices/<id>` | `{"id":<id>,"state":"On"\|"Off"}` | Synthetic only; tested CC has none |

`GET /api/world` returns everything the bridge publishes in one response, including `modes` (`AtHome`, `Vacation`, `Pause`, each with `state`; the active one with `startTime`/`endTime`), `temperatureSettings` (pause and vacation setpoint with min/max) and `systemInfo`.

Observed CC behavior that shapes the client:

- **Pause and Vacation block setpoint writes.** The CC answers 400 *"Setpoint can only be changed while room is not in paused or vacation mode."* and holds rooms at the pause/vacation temperature. The client refuses such writes from a fresh read before sending. A CC left in Pause looks like lost control.
- **Regulation type is zone-governed.** `PATCH /api/rooms/<id>` with `{"regulation":{"type":…}}` for a room in a zone returns 400 *"Can't change regulation type of rooms that are part of the Living Zone."* It is therefore not on the allowlist; heat/auto is read-only.
- Rejections carry `content.errorMessages`; the bridge publishes them (printable, at most four, 200 characters each) in the **Last command** entity.

No schedule, firmware, alarm acknowledgement, zone or regulation-type writes are implemented.

`allRooms` includes `id`, `name`, `ambientTemperature`, `minSetpoint`, `maxSetpoint`, `regulation`, `regulationState`, `setpoint`, `state`, `zone`, `mode`, and `hasTemperatureSensor` in the inspected live response. `777` is an app sentinel for unavailable temperatures and is not published as a temperature.

Client-side temperature limits are 6–35 °C intersected with the freshly read room bounds. Commands use actual room IDs, not display positions. After one PATCH, a fresh room read must match the requested value within 0.05 °C. An already-matching setpoint does not generate another PATCH.

## Pairing compatibility observations

- The app uses a BrainpoolP320r1 application identity. The matching profile is a self-issued v3 certificate, serial 1, C=DK/O=Trifork/CN=localhost, SHA-256, SKI and AKI, and critical CA:FALSE.
- The original Lua PACE nonce-decryption path passes the IV as BN userdata. Its native string conversion returns NULL, leaving a zero IV. Only this specific PACE nonce-decode operation reproduces that behavior; generic AES uses its supplied IV.
- Peer proofs are checked before saving trust. The interoperable proof construction is visible in the Python implementation; the source is not encoded or hidden.
- An unsuccessful self-signature probe of an issuer-signed certificate must not leave OpenSSL errors queued before subsequent pinned TLS reads.

These behaviors were compared with offline synthetic tests and a successful owner-initiated PACE pairing. No vendor binary/decompiled source is included in this repository.

## Battery and alarms

The inspected `/api/world` response had no individual thermostat list or battery-level field. Multiple physical thermostats can share a room; a room climate entity is not a physical-device battery entity.

The app supports low and critical battery warnings. Alarm parsing includes `id`, `isRead`, `createdDate`, `level`, `room`, `node`, `titleTextId` and `descriptionTextId`. Whether `node` is a stable, unique physical-thermostat identifier remains unverified: the tested installation had zero active alarms. An empty alarm list is not a battery percentage or proof that every device is reachable.

`systemStatus.currentState` is published as received; `Asleep` is not interpreted as cloud disconnection or a powered-off CC. `Heating`/`Deadband` are room regulation states, not valve position or measured water flow. The exact deadband thresholds and `ForecastHeating` behavior remain unverified.
