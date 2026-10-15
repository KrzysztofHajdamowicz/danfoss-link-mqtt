# Pairing and trust

Pair only your own CC using the fresh cloud code shown on its panel. The client creates its own application key and certificate; it does not import another phone's private key. The `--name` parameter is the name shown in the CC's list of paired apps.

A new state volume must first be initialized for UID/GID 65534:

```sh
docker compose run --rm state-init
```

The helper is behind the `setup` profile and does not run during normal deployments. It changes ownership/mode of the state root and profile directory, not arbitrary host files.

Pairing:

```sh
docker compose run --rm -it mqtt python pair_cc.py --state /state --apk-identity-test --name "Danfoss MQTT"
```

`--apk-identity-test` selects the app-compatible certificate profile under `/state/apk-profile`; the historical flag name is retained for compatibility. The code is entered with `getpass`, is not echoed, and is not saved. Codes starting with `0000` are local pairing codes and are refused by this cloud client. After success, the runtime uses the same profile directory without another panel code.

## Authentication layers

1. The grid/control connection validates the server chain using the application's public trust anchors.
2. The authenticated grid provides the router endpoint and temporary routing token.
3. During initial pairing only, the CC's issuer may be missing. The TLS candidate remains untrusted; panel-code PACE must verify the peer proof, bound to the exchanged TLS public keys, before trust is stored.
4. A successful PACE exchange saves the CC certificate and peer identifier. Failed pairing does not save new CC trust.
5. Normal operation requires the exact saved CC certificate, plus the implemented validity/key checks. The missing-issuer pairing bootstrap is not used as an alternative to a mismatched saved pin.

The narrow OpenSSL patch supports mathematically exact BrainpoolP320r1 keys in the legacy explicit certificate encoding. It does not disable general chain, signature or expiry checks, does not lower the global TLS security level, and does not install crypto libraries on the host.

## Private files

`app_key.pem`, `app_cert.pem`, and `paired_cc.json` live in the Docker volume. The directory must belong to the runtime UID and be mode 0700; credential/trust files must be mode 0600. The client rejects unsafe state permissions. An optional local public-certificate diagnostic record may also be written during pairing; it belongs in private state, not Git.

Back up this volume securely. Do not commit it, include it in the Docker build context, paste it in issues, or delete it during routine upgrades. A lost identity or changed CC certificate requires deliberate investigation/re-pairing; the client does not silently replace saved trust.
