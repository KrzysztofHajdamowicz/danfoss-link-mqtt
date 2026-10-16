# Danfoss Link MQTT

Connect a Danfoss Link CC to Home Assistant through MQTT Discovery, using the existing Danfoss cloud as an additional paired client. No CC firmware changes and no Home Assistant custom component are required.

This is an independent, experimental project, not an official Danfoss product. It depends on the continued availability of the cloud service. Initial testing used Link CC MK 4.1, firmware 4.2.1724, and protocol behavior observed in Android app 1.3.10. Other hardware and firmware are not yet verified.

## Features

- Climate entities with room names, current temperature and target temperature.
- Setpoint changes with GET readback; uncertain writes are not automatically retried.
- Current heating action: `Heating` → `heating`, `Deadband` → `idle`, `Cooling` → `cooling`. Unknown states reset the action rather than guessing.
- System status sensor displaying the CC's `systemStatus.currentState`, for example `Asleep`.
- Alarm problem sensor, alarm count sensor and active alarm details as attributes.
- MQTT availability, retained discovery/state, and rediscovery when Home Assistant restarts.
- Only `MQTT_HOST` is required. Default connection: port 1883, without authentication or TLS. Optional settings are documented in [configuration](docs/configuration.md).

Room reads and temperature writes have been verified on one installation, including HA → CC and CC → HA. System/alarm fields have been read from the CC, but the new automatic status/alarm publication still needs installation-level confirmation. No battery percentage or complete per-thermostat device list has been found; see [API findings](docs/api.md).

## Prebuilt image

GitHub Actions builds `linux/amd64`, runs the Python/MQTT tests and then runs native TLS tests inside the built image. Only that tested image is published to `ghcr.io/leifnel/danfoss-link-mqtt:latest` and `:sha-<full-commit-sha>`. A failed build or test does not update the published tags. See [image distribution](docs/images.md) for first-publication visibility and deployment.

Use `compose.image.yaml` for the prebuilt image, or `compose.yaml` to build locally. Both preserve the same volume layout and MQTT entity IDs. The first publication must finish before the image can be pulled.

## Quick start: a new installation

The Docker build currently targets **linux/amd64**. It builds a private, narrowly patched OpenSSL inside the image; it does not replace the host's or Home Assistant's crypto libraries. All source and the patch are readable files in this repository. The first image build can take several minutes.

```sh
git clone https://github.com/leifnel/danfoss-link-mqtt.git
cd danfoss-link-mqtt
cp .env.example .env
```

Set `MQTT_HOST` in `.env` to your broker's hostname or IP. Home Assistant's MQTT integration must use the same broker.

```sh
docker compose build mqtt
docker compose run --rm state-init
docker compose run --rm -it mqtt python pair_cc.py --state /state --apk-identity-test --name "Danfoss MQTT"
```

Open the remote/app connection menu on your own CC panel, generate a fresh cloud pairing code, and enter it at the hidden terminal prompt. Do not put the code in command-line arguments, environment variables or issue reports. Hyphens in the displayed code are accepted. See [pairing and trust](docs/pairing.md).

After successful pairing:

```sh
docker compose up -d mqtt
docker logs -f danfoss-link-mqtt
```

Home Assistant discovers the rooms and three controller sensors under **Danfoss Link CC**. Verify the readings first. Temperature commands are enabled by default; set `DANFOSS_ALLOW_WRITES=false` for read-only operation.

## Existing pairing

**Do not re-pair an already working installation.** Set `DANFOSS_STATE_VOLUME` to the exact existing Docker volume name before starting the new checkout. The profile remains `/state/apk-profile`. See [migration guide](docs/migration.md) before changing an existing stack.

Updating source does not update an existing image until it is rebuilt:

```sh
git pull --ff-only
docker compose build mqtt
docker compose up -d --no-deps mqtt
```

The container name stays `danfoss-link-mqtt`. Never use `docker compose down -v` unless you intentionally want to remove pairing credentials.

## Development

Use Python 3.12 and a virtual environment:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
PYTHONPATH=src:tests python -m unittest test_mqtt_bridge test_mqtt_status test_mqtt_wire test_temperature_control test_readonly_transport test_pace_crypto test_identity_profile test_code_diagnostics test_rooms
```

Tests use synthetic data and certificates. The wire test starts a temporary localhost MQTT broker and mocks the Danfoss transport; it does not contact a real CC. Native TLS regressions need the private OpenSSL Docker image; see [testing](docs/testing.md).

## Repository contents

- `src/`: Python client, paired transport and MQTT bridge.
- `patches/`: reviewable OpenSSL compatibility patch.
- `tests/`: synthetic protocol, crypto and MQTT tests.
- `docs/`: setup, API evidence and trust model.
- `Dockerfile`, `compose.yaml`: normal source build, with no embedded ZIP/base64 source bundles.

The APK, decompiled vendor sources, private pairing identities, live device certificates, raw household captures and credentials are not distributed here. Base64/hex used by the actual wire protocols or certificate storage is ordinary data encoding, not hidden application source.

## License

Project code is MIT licensed. OpenSSL and Paho retain their own licenses. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
