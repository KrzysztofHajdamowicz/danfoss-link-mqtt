# Changelog

## 0.3.0

### Dependencies
* Bump astral-sh/uv from 0.13.1 to 0.13.6 by @dependabot[bot] in https://github.com/KrzysztofHajdamowicz/danfoss-link-mqtt/pull/2
* Bump amqtt from 0.11.3 to 0.12.1 by @dependabot[bot] in https://github.com/KrzysztofHajdamowicz/danfoss-link-mqtt/pull/3
### Changes
* Hub and room devices, Home Assistant add-on, supply-chain-checked Alpine image by @KrzysztofHajdamowicz in https://github.com/KrzysztofHajdamowicz/danfoss-link-mqtt/pull/1
* CI: run the push-triggered Tests workflow on main only by @KrzysztofHajdamowicz in https://github.com/KrzysztofHajdamowicz/danfoss-link-mqtt/pull/4
* Pair from the add-on configuration or a code file by @KrzysztofHajdamowicz in https://github.com/KrzysztofHajdamowicz/danfoss-link-mqtt/pull/5
* Release from a version tag by @KrzysztofHajdamowicz in https://github.com/KrzysztofHajdamowicz/danfoss-link-mqtt/pull/6

### New Contributors
* @KrzysztofHajdamowicz made their first contribution in https://github.com/KrzysztofHajdamowicz/danfoss-link-mqtt/pull/1
* @dependabot[bot] made their first contribution in https://github.com/KrzysztofHajdamowicz/danfoss-link-mqtt/pull/2

**Full Changelog**: https://github.com/KrzysztofHajdamowicz/danfoss-link-mqtt/commits/v0.3.0

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
