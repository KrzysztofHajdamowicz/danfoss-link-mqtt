# Danfoss Link MQTT

Connect a Danfoss Link CC to Home Assistant through MQTT discovery, using the existing Danfoss cloud as an additional paired client. No CC firmware changes and no Home Assistant custom component are required. Runs as a **Home Assistant add-on** or as a standalone container.

This is a fork of [leifnel/danfoss-link-mqtt](https://github.com/leifnel/danfoss-link-mqtt), which provides the cloud transport, pairing and patched OpenSSL runtime. The fork adds a hub-and-rooms device model, more verified controls, the add-on and a supply-chain-checked image. It is an independent, experimental project, not an official Danfoss product, and depends on the continued availability of the Danfoss (Trifork) relay. Tested with Link CC firmware 4.2.1724 and protocol behavior observed in Android app 1.3.10.

## Features

- **Hub device** with Pause, Holiday and At home switches, holiday start/end, pause and holiday temperatures, system status, alarms and the result of the last command.
- **One device per CC room**, connected via the hub: thermostat (current and target temperature, heating action, manual/schedule shown as heat/auto), temperature sensor, at-home and away setpoints; schedule and zone as attributes.
- MQTT **device discovery** with availability, retained state, rediscovery when Home Assistant restarts, and automatic migration of entities from the earlier flat model.
- Every write: fresh read, one allowlisted request, readback. Uncertain writes are never retried; Pause/Holiday-blocked setpoints are refused before sending; slider bursts are coalesced.
- Read-only mode (`DANFOSS_ALLOW_WRITES=false`).

See [configuration](docs/configuration.md) for all entities and topics and [API findings](docs/api.md) for what has been verified on a real CC.

## Home Assistant add-on

[![Add the repository to your Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FKrzysztofHajdamowicz%2Fdanfoss-link-mqtt)
[![Open the Danfoss Link CC (MQTT) add-on in your Home Assistant](https://my.home-assistant.io/badges/supervisor_app.svg)](https://my.home-assistant.io/redirect/supervisor_app/?app=e7f0e068_danfoss_link_cc_mqtt&repository_url=https%3A%2F%2Fgithub.com%2FKrzysztofHajdamowicz%2Fdanfoss-link-mqtt)

1. Add this repository with the first button, or manually: `https://github.com/KrzysztofHajdamowicz/danfoss-link-mqtt` under the add-on (app) store's **⋮ → Repositories**.
2. Open **Danfoss Link CC (MQTT)** with the second button and install it.
3. Pair or import an existing identity as described in the [add-on documentation](danfoss_link_cc_mqtt/DOCS.md).

Requires Home Assistant 2026.5 or newer and an MQTT broker (the Mosquitto add-on is used automatically). The add-on is amd64-only for now.

## Standalone container

The image builds a private, narrowly patched OpenSSL inside the image; it does not replace the host's or Home Assistant's crypto libraries. All source and the patch are readable files in this repository. A prebuilt amd64 image is published as `ghcr.io/krzysztofhajdamowicz/danfoss-link-mqtt` (see [images](docs/images.md)); the Dockerfile also builds natively on arm64.

```sh
git clone https://github.com/KrzysztofHajdamowicz/danfoss-link-mqtt.git
cd danfoss-link-mqtt
cp .env.example .env
```

Set `MQTT_HOST` in `.env` to your broker's hostname or IP. Home Assistant's MQTT integration must use the same broker.

```sh
docker compose build mqtt
docker compose run --rm state-init
docker compose run --rm -it mqtt python pair_cc.py --state /state --apk-identity-test --name "Danfoss MQTT"
```

Open the remote/app connection menu on your own CC panel, generate a fresh cloud pairing code, and enter it at the hidden terminal prompt. Do not put the code in command-line arguments, environment variables or issue reports. See [pairing and trust](docs/pairing.md).

```sh
docker compose up -d mqtt
docker logs -f danfoss-link-mqtt
```

Verify readings first. **Do not re-pair an already working installation**: set `DANFOSS_STATE_VOLUME` to the existing volume (see [migration](docs/migration.md)). Never run two bridges with the same pairing at once, and never use `docker compose down -v` unless you intend to delete the pairing.

## Development

Dependencies are managed with [uv](https://docs.astral.sh/uv/) and locked in `uv.lock`:

```sh
uv sync --locked
PYTHONPATH=src:tests uv run python -m unittest test_hub_transport test_hub_model test_hub_client test_hub_bridge test_hub_wire
```

The full module list and the native TLS tests that need the private OpenSSL image are in [testing](docs/testing.md). Tests use synthetic data and certificates and never contact a real CC.

## Repository contents

- `src/`: cloud transport (`danfoss_mdg/`), hub bridge (`hub_*.py`), flat bridge (`mqtt_bridge.py`), pairing CLI and add-on entrypoint.
- `danfoss_link_cc_mqtt/`, `repository.yaml`: Home Assistant add-on repository.
- `patches/`: reviewable OpenSSL compatibility patch; `tools/`: source fetch and SBOM annotation.
- `tests/`: synthetic protocol, crypto, MQTT and add-on tests.
- `docs/`: setup, API evidence, images and trust model.

The APK, decompiled vendor sources, private pairing identities, live device certificates, raw household captures and credentials are not distributed here.

## License

Project code is MIT licensed. OpenSSL and Paho retain their own licenses. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
