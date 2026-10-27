# Prebuilt images

The **Build, test and publish images** workflow runs for pull requests, after a push to `main`, or from GitHub Actions' Run workflow button. It runs the Python suite (including the temporary MQTT broker), builds both amd64 images once and runs the native TLS, PACE, hub and add-on tests inside each of them. Pull requests stop there. On `main`, only those tested images are tagged, pushed to GitHub Container Registry and attested.

| Image | Tags | Use |
| --- | --- | --- |
| `ghcr.io/krzysztofhajdamowicz/danfoss-link-mqtt` | `latest`, `sha-<full-commit-sha>` | Standalone container (`compose.image.yaml`) |
| `ghcr.io/krzysztofhajdamowicz/amd64-danfoss-link-cc-mqtt` | `<add-on version>`, `sha-<full-commit-sha>` | Home Assistant add-on |

Both are built from the same `Dockerfile` (targets `standalone` and `addon`) on `python:3.12-alpine`, with Python dependencies installed by `uv` from `uv.lock` and the private OpenSSL trimmed to its shared libraries and CLI. An add-on version tag is never overwritten: bump `version` in `danfoss_link_cc_mqtt/config.yaml` (and `pyproject.toml`, `src/hub_model.py`; a test keeps them equal) to release a new add-on build.

No device state, keys, MQTT passwords or private certificates are used by the workflow. Publishing uses the repository-scoped `GITHUB_TOKEN`. Base images, the uv image and all actions are pinned by digest or commit SHA.

## Supply chain

- **SBOM:** syft produces a CycloneDX 1.6 JSON SBOM for each image. `tools/sbom_openssl.py` then records the privately built OpenSSL with the verified source archive hash and the brainpool patch as CycloneDX `pedigree`, and the SBOM is validated against the 1.6 schema. SBOMs are uploaded as the `sbom-cyclonedx-1.6` workflow artifact on every run.
- **Attestations:** on `main`, each pushed image gets a SLSA build provenance attestation and an SBOM attestation, both stored in the registry next to the image and in the repository's attestations.

  ```sh
  gh attestation verify oci://ghcr.io/krzysztofhajdamowicz/danfoss-link-mqtt:latest \
    --owner KrzysztofHajdamowicz
  gh attestation verify oci://ghcr.io/krzysztofhajdamowicz/danfoss-link-mqtt:latest \
    --owner KrzysztofHajdamowicz --predicate-type https://cyclonedx.org/bom
  ```

- **Updates:** Dependabot proposes `uv`, Docker and GitHub Actions updates after a 7-day cooldown. Patch and minor updates, and refreshed digests of the same Docker tag, are merged automatically once the required checks pass; major updates and new Python versions wait for review. OpenSSL is outside Dependabot's reach, so a weekly workflow reports newer 3.5.x releases.

## First publication visibility

GitHub Container Registry initially creates packages as private, even for a public source repository. After the first successful publication, open the settings of both packages and change their visibility to **Public**; otherwise anonymous Docker pulls and Home Assistant add-on installs fail.

```sh
docker pull ghcr.io/krzysztofhajdamowicz/danfoss-link-mqtt:latest
```

## Install or migrate (standalone)

`compose.image.yaml` uses the image, not a local build context. Set only `MQTT_HOST` for a default broker connection. Existing installations must also select their existing `DANFOSS_STATE_VOLUME` to keep pairing. New installations use the setup helper and pairing procedure documented in README, with `-f compose.image.yaml` on the Compose commands.

```sh
docker compose -f compose.image.yaml pull mqtt
docker compose -f compose.image.yaml up -d --no-deps mqtt
```

To pin a build, set `DANFOSS_IMAGE=ghcr.io/krzysztofhajdamowicz/danfoss-link-mqtt:sha-<full-commit-sha>`. The container remains `danfoss-link-mqtt`; the pairing volume is not deleted or reinitialized.
