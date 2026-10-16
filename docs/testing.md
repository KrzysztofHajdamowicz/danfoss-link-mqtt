# Testing and verification limits

The quick Python suite uses synthetic requests/responses, temporary credentials/certificates and a temporary localhost MQTT broker. Its Danfoss transport is mocked, so no household state is changed by those tests.

The private runtime has a separate TLS regression suite that generates its own synthetic certificates. Run after building the image:

```sh
docker build -t danfoss-link-mqtt:test .
docker run --rm --network none --read-only \
  --tmpfs /tmp:rw,nosuid,size=32m,mode=1777 \
  -e PYTHONPATH=/app:/app/tests \
  danfoss-link-mqtt:test python -m unittest test_native_tls test_pace_crypto test_mqtt_status
```

Native TLS tests exercise explicit BrainpoolP320r1, ordinary named curves, peer pinning, issuer handling, corrupted signatures, expiration and encrypted memory-BIO traffic. They require the patched runtime; do not expect them to pass against unmodified system OpenSSL. The image workflow runs on pushes to main or manual dispatch. It runs the Python suite first, builds the image, runs native tests inside that exact image, and publishes only if all steps pass. The ordinary push/PR workflow runs the Python suite and synthetic MQTT broker test.

Initial public-source preparation ran 42 Python tests successfully. The readable patch applied cleanly, with no fuzz, to the previously retained OpenSSL 3.5.9 source files. This workspace has no Docker engine, so the newly reorganized Dockerfile has not been built here. The same compatibility patch/runtime was previously built and exercised on the original test host; the new file layout needs a fresh build confirmation.

Installation verification should begin with reads: confirm room names, measured temperatures, setpoints and system status. Only then test a temperature change in a room chosen by its owner and verify it on the CC/app. A failed/uncertain write must not be blindly retried.

Live evidence so far comes from one installation: successful pairing, reconnect with the saved certificate, reads of nine rooms and a temperature write with readback; the owner also confirmed HA → CC and CC → HA. Automatic heating-action/system/alarm publication has synthetic coverage but still needs owner confirmation on the updated installation. Actual active low-battery alarms and multi-thermostat alarm identification remain unverified.
