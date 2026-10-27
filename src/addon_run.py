"""Home Assistant add-on entrypoint: map options, find MQTT, import or await pairing."""

import json, os, stat, sys, tempfile, time, urllib.request
from pathlib import Path

OPTIONS = Path("/data/options.json")
STATE = Path("/data/apk-profile")
CONFIG = Path("/config")  # addon_config:rw
IDENTITY = ("app_key.pem", "app_cert.pem", "paired_cc.json")
BRIDGE = Path(__file__).with_name("hub_bridge.py")


def load_options(path=OPTIONS):
    try:
        options = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return options if isinstance(options, dict) else {}


def supervisor_mqtt():
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    request = urllib.request.Request(
        "http://supervisor/services/mqtt",
        headers={"Authorization": "Bearer " + token},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.load(response).get("data")
    except (OSError, ValueError, AttributeError):
        return None
    return data if isinstance(data, dict) and data.get("host") else None


def bridge_env(options, service=None, base=None):
    """Translate add-on options to the bridge's environment; None if no broker."""
    env = dict(os.environ if base is None else base)
    host = str(options.get("mqtt_host") or "").strip()
    if host:
        broker = {
            "host": host,
            "port": options.get("mqtt_port"),
            "username": options.get("mqtt_username"),
            "password": options.get("mqtt_password"),
            "ssl": options.get("mqtt_tls"),
        }
    elif service:
        broker = service
    else:
        return None
    env["MQTT_HOST"] = str(broker["host"])
    env["MQTT_PORT"] = str(broker.get("port") or "")
    env["MQTT_USERNAME"] = str(broker.get("username") or "")
    env["MQTT_PASSWORD"] = str(broker.get("password") or "")
    env["MQTT_TLS"] = "true" if broker.get("ssl") else "false"
    env["MQTT_TOPIC_PREFIX"] = str(options.get("topic_prefix") or "")
    env["MQTT_DISCOVERY_PREFIX"] = str(options.get("discovery_prefix") or "")
    env["DANFOSS_POLL_INTERVAL"] = str(options.get("poll_interval") or "")
    env["DANFOSS_ALLOW_WRITES"] = "false" if options.get("allow_writes") is False else "true"
    env["DANFOSS_SUGGEST_AREAS"] = "false" if options.get("suggest_areas") is False else "true"
    if options.get("timezone"):
        env["DANFOSS_TIMEZONE"] = str(options["timezone"])
    return env


def _identity_source(config):
    for source in (config / "apk-profile", config):
        files = [source / name for name in IDENTITY]
        if all(path.exists() for path in files):
            return files
    return None


def import_identity(state=STATE, config=CONFIG):
    """Copy a paired identity dropped into the add-on config folder; never overwrite."""
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    state.chmod(0o700)
    if (state / "paired_cc.json").exists():
        return False
    files = _identity_source(config)
    if files is None:
        return False
    for path in files:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= 65535:
            print("Ignoring identity files: expected small regular files", flush=True)
            return False
    # paired_cc.json last: its presence marks the profile complete.
    for path in files:
        with tempfile.NamedTemporaryFile(dir=state, delete=False) as file:
            os.fchmod(file.fileno(), 0o600)
            file.write(path.read_bytes())
            file.flush()
            os.fsync(file.fileno())
        os.replace(file.name, state / path.name)
    print(
        "Imported paired identity; delete the copies from the add-on config folder",
        flush=True,
    )
    return True


def pair_with_option(code, state=STATE):
    """Pair with the pairing_code option; the code is never logged."""
    from pair_cc import pair_with_code

    if pair_with_code(state, code, "Home Assistant"):
        print(
            "Paired. You can remove the pairing code from the add-on configuration.",
            flush=True,
        )


def wait_until_paired(state=STATE, config=CONFIG, interval=5, code=None):
    if (state / "paired_cc.json").exists():
        if code:
            print(
                "Already paired; the pairing code option is ignored and can be removed",
                flush=True,
            )
        return
    if code:
        pair_with_option(code, state)
        if (state / "paired_cc.json").exists():
            return
    print(
        "NOT PAIRED. Either generate a fresh cloud code on the CC panel, enter it as the "
        "pairing code option and restart the add-on; or copy app_key.pem, app_cert.pem "
        "and paired_cc.json into this add-on's config folder; or pair from the host shell:\n"
        "  docker exec -it $(docker ps -qf name=danfoss_link_cc_mqtt) \\\n"
        '    python /app/pair_cc.py --state /data --apk-identity-test --name "Home Assistant"\n'
        "The bridge starts automatically once pairing is saved.",
        flush=True,
    )
    while not (state / "paired_cc.json").exists():
        time.sleep(interval)
        import_identity(state, config)
    print("Pairing detected; starting bridge", flush=True)


def main():
    options = load_options()
    env = bridge_env(options, None if options.get("mqtt_host") else supervisor_mqtt())
    if env is None:
        print(
            "No MQTT broker: install the Mosquitto add-on or set mqtt_host in the options",
            flush=True,
        )
        return 1
    print("Using MQTT broker " + env["MQTT_HOST"], flush=True)
    import_identity()
    wait_until_paired(code=str(options.get("pairing_code") or "").strip() or None)
    os.execve(sys.executable, [sys.executable, str(BRIDGE), "--state", str(STATE)], env)


if __name__ == "__main__":
    sys.exit(main())
