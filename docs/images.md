# Prebuilt image

The **Build, test and publish image** workflow runs after a push to `main`, or from GitHub Actions' Run workflow button. It first runs the Python suite (including the temporary MQTT broker), builds the amd64 image and runs native TLS/status regressions inside it. Only after success is that same image tagged and pushed to GitHub Container Registry.

Published tags:

- `ghcr.io/leifnel/danfoss-link-mqtt:latest`: latest successfully tested main-branch build.
- `ghcr.io/leifnel/danfoss-link-mqtt:sha-<full-commit-sha>`: source-specific build tag for deliberate upgrades/rollback.

The registry digest identifies the exact downloaded image. No device state, keys, MQTT passwords or private certificates are used by the workflow. Publishing uses the repository-scoped `GITHUB_TOKEN`, not a manually supplied password. The first build compiles OpenSSL on GitHub; later builds can reuse its build cache. No ARM image is published yet.

## First publication visibility

GitHub Container Registry initially creates packages as private, even for a public source repository. After the first successful publication, the repository owner may need to open their `danfoss-link-mqtt` package settings and change package visibility to **Public** before anonymous Docker pulls work. The OCI source label associates the image with this repository. No users should need a personal token to pull the intended public image.

Check availability with:

```sh
docker pull ghcr.io/leifnel/danfoss-link-mqtt:latest
```

Do not switch a working deployment until the build has succeeded and the image can be pulled.

## Install or migrate

`compose.image.yaml` is standalone: it uses the image, not a local build context. Set only `MQTT_HOST` for a default broker connection. Existing installations must also select their existing `DANFOSS_STATE_VOLUME` to keep pairing. New installations use the setup helper and pairing procedure documented in README, with `-f compose.image.yaml` on the Compose commands.

For an already paired installation:

```sh
docker compose -f compose.image.yaml pull mqtt
docker compose -f compose.image.yaml up -d --no-deps mqtt
```

To pin a specific published build, set `DANFOSS_IMAGE=ghcr.io/leifnel/danfoss-link-mqtt:sha-<full-commit-sha>` in the environment. Set it back to `ghcr.io/leifnel/danfoss-link-mqtt:latest` to follow tested main builds. The container remains `danfoss-link-mqtt`; the pairing volume is not deleted or reinitialized.

The prebuilt image does not require a local build. Pull and deploy it with the commands above after a new image is published. No Home Assistant restart or re-pairing is normally needed.
