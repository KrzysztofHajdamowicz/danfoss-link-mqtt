# Third-party components

- OpenSSL 3.5.9: https://github.com/openssl/openssl — Apache License 2.0. The Docker build downloads the official source, verifies a pinned SHA-256, applies the reviewable patch in `patches/`, and includes OpenSSL's LICENSE.txt in the runtime image. Patch context comes from OpenSSL; upstream notices and license continue to apply to it.
- Eclipse Paho MQTT Python 2.1.0: https://github.com/eclipse-paho/paho.mqtt.python — EPL-2.0 / EDL-1.0 licensing; see its distribution for full terms.
- aMQTT 0.11.3: https://github.com/Yakifo/amqtt — test-only dependency; its own license and dependency licenses apply.
- `src/danfoss_mdg/trusted-CAs.pem` contains public service trust anchors required for MDG interoperability. They contain no private keys and are not a household's paired CC certificate. Their inclusion does not grant rights to vendor trademarks or establish affiliation.

The project does not distribute the original app APK, vendor native libraries, or decompiled Java/Lua source. Protocol field descriptions are included for interoperability. Danfoss and other product names belong to their respective owners.
