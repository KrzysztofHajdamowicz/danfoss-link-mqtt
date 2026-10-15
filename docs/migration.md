# Existing installations and Komodo

The earlier research deployment embedded ZIP/base64 source inside one Compose file. This repository replaces that packaging with readable source, a normal Dockerfile and a readable patch. It does not change the existing pairing record format or MQTT unique IDs.

## Reuse a working pairing volume

Identify your old volume locally. Set these variables in `.env` or the Komodo stack environment:

```dotenv
MQTT_HOST=your-broker-host
DANFOSS_STATE_VOLUME=your-existing-pairing-volume
```

Do not start with the default new volume name if you intend to reuse an existing pairing. Verify the exact name with `docker volume ls`. The application state path remains `/state/apk-profile` and the runtime UID is 65534. Existing safe state needs no initialization or re-pairing.

Do not run the old and new deployments simultaneously with the same identity: they use the same MQTT client ID and container name. Stop the old deployment without deleting its volume, then deploy the checked-out source using the existing volume.

## Komodo

Use a Git-backed stack pointing at this repository, with `compose.yaml` at the repository root. The complete checkout must be available as the Docker build context; copying this short Compose file alone into an otherwise empty stack directory is insufficient.

Set `MQTT_HOST` in the stack environment. For migration, also set `DANFOSS_STATE_VOLUME`. Save the configuration, update/pull the checkout and build the MQTT service before deploying. Komodo versions/configurations differ in whether Deploy rebuilds an existing image. If needed, from the actual checkout directory on the Docker host:

```sh
docker compose build mqtt
docker compose up -d --no-deps mqtt
```

To verify that the status/alarm version is present:

```sh
docker exec danfoss-link-mqtt python -c "from mqtt_bridge import clean_world; print('Status and alarm code installed')"
docker logs --tail 80 danfoss-link-mqtt
```

The container is replaced under the same name, `danfoss-link-mqtt`. The volume is preserved. Wait for a successful cloud refresh, then check the nine-room installation's existing climate entities and the three new controller sensors. Actual room counts depend on the installation.
