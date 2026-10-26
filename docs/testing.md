# Testing and verification limits

The Python suite uses synthetic requests/responses, temporary credentials/certificates and a temporary localhost MQTT broker. Its Danfoss transport is mocked — the hub tests use an in-memory CC (`tests/hub_fixtures.py`) that enforces the same request allowlist and the Pause/Holiday setpoint rule — so no household state is changed by those tests.

```sh
uv sync --locked
PYTHONPATH=src:tests uv run python -m unittest test_mqtt_bridge test_mqtt_status test_mqtt_wire \
  test_temperature_control test_readonly_transport test_pace_crypto test_identity_profile \
  test_code_diagnostics test_rooms test_hub_transport test_hub_model test_hub_client \
  test_hub_bridge test_hub_wire test_addon_run test_sbom_openssl
```

The private runtime has a separate TLS regression suite that generates its own synthetic certificates. Tests are not part of the image; mount them into the exact image under test:

```sh
docker build -t danfoss-link-mqtt:test .
docker run --rm --network none --read-only \
  --tmpfs /tmp:rw,nosuid,size=32m,mode=1777 \
  -v "$PWD/tests:/app/tests:ro" -e PYTHONPATH=/app:/app/tests \
  danfoss-link-mqtt:test python -m unittest test_native_tls test_pace_crypto test_mqtt_status \
  test_hub_transport test_hub_client test_hub_bridge test_addon_run
```

Native TLS tests exercise explicit BrainpoolP320r1, ordinary named curves, peer pinning, issuer handling, corrupted signatures, expiration and encrypted memory-BIO traffic. They require the patched runtime; do not expect them to pass against unmodified system OpenSSL. On Alpine (musl), `ctypes.util.find_library` cannot locate libraries, so the PACE and certificate helpers fall back to loading `libcrypto.so.3` by name, which resolves to the private runtime through `LD_LIBRARY_PATH`.

The amqtt test broker does not preserve one client's QoS 1 publish order when forwarding and its shutdown can hang; the hub wire test therefore subscribes with QoS 0, checks the shutdown state at the bridge's client and bounds the broker shutdown. Production brokers such as Mosquitto keep order.

Installation verification should begin with reads: confirm room names, measured temperatures, setpoints and system status. Only then test a change in a room chosen by its owner and verify it on the CC. A failed/uncertain write must not be blindly retried.

Live evidence comes from two installations. The original one: pairing, reconnect with the saved certificate, reads of nine rooms and a temperature write with readback, HA → CC and CC → HA. A second one (6 rooms, firmware 4.2.1724): pairing, `/api/world` reads, room setpoint, at-home/away setpoints, pause/holiday temperatures, and leaving Pause, Holiday and At home. Activating Holiday or At home, on/off devices, actual low-battery alarms and multi-thermostat alarm identification remain unverified on a real CC.
