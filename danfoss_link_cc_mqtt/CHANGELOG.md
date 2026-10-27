# Changelog

## 0.2.0

- Pair from the add-on configuration: enter a fresh cloud code from the CC panel as the
  `pairing_code` option. A code is tried once and never logged; it is ignored once paired.
- Standalone image: pair at startup from a code file such as a Docker secret
  (`DANFOSS_PAIRING_CODE_FILE`).

## 0.1.0

- First add-on release: hub and per-room devices via MQTT device discovery, verified commands for
  room setpoints, at-home/away setpoints, Pause, Holiday, At home and pause/holiday temperatures.
- Automatic migration of entities from the earlier flat MQTT model.
- Alpine-based image with the private brainpoolP320r1 OpenSSL 3.5.9 runtime.
