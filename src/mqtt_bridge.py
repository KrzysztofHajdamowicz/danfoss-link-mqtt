"""Danfoss Link to MQTT discovery, with bounded non-retained commands."""

import argparse, asyncio, hashlib, json, math, os, re, signal, time, tempfile, stat
from dataclasses import dataclass, field
from pathlib import Path
from danfoss_client import DanfossClient, numeric
from danfoss_mdg.control_transport import PairedControlTransport
from danfoss_mdg.readonly_transport import load_trust
from rooms import clean_rooms
from danfoss_readonly import read_request, response_content, ProtocolError


@dataclass(frozen=True)
class Settings:
    host: str
    port: int = 1883
    username: str = ""
    password: str = field(default="", repr=False)
    tls: bool = False
    ca_file: str = ""
    prefix: str = "danfoss_link"
    discovery_prefix: str = "homeassistant"
    poll_interval: int = 60
    allow_writes: bool = True

    @classmethod
    def from_env(cls):
        host = os.environ.get("MQTT_HOST", "").strip()
        tls = os.environ.get("MQTT_TLS", "false").lower() in ("1", "true", "yes")
        port = int(os.environ.get("MQTT_PORT") or ("8883" if tls else "1883"))
        poll = int(os.environ.get("DANFOSS_POLL_INTERVAL") or "60")
        prefix = os.environ.get("MQTT_TOPIC_PREFIX") or "danfoss_link"
        discovery = os.environ.get("MQTT_DISCOVERY_PREFIX") or "homeassistant"
        valid = lambda s: bool(re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*", s))
        if (
            not host
            or len(host) > 253
            or not 1 <= port <= 65535
            or not 30 <= poll <= 3600
            or not valid(prefix)
            or not valid(discovery)
        ):
            raise ValueError("Invalid MQTT settings")
        return cls(
            host,
            port,
            os.environ.get("MQTT_USERNAME", ""),
            os.environ.get("MQTT_PASSWORD", ""),
            tls,
            os.environ.get("MQTT_CA_FILE", ""),
            prefix,
            discovery,
            poll,
            (os.environ.get("DANFOSS_ALLOW_WRITES") or "true").lower()
            in ("1", "true", "yes"),
        )


def discovery(config, controller_id, room):
    base = config.prefix + "/" + controller_id
    topic = base + "/room/" + str(room["id"])
    payload = {
        "name": room["name"],
        "unique_id": "danfoss_link_" + controller_id + "_room_" + str(room["id"]),
        "device": {
            "identifiers": ["danfoss_link_" + controller_id],
            "name": "Danfoss Link CC",
            "manufacturer": "Danfoss",
            "model": "Link CC",
        },
        "availability_topic": base + "/availability",
        "payload_available": "online",
        "payload_not_available": "offline",
        "current_temperature_topic": topic + "/state",
        "current_temperature_template": "{{ value_json.temperature if value_json.temperature is not none else 'None' }}",
        "temperature_state_topic": topic + "/state",
        "temperature_state_template": "{{ value_json.setpoint if value_json.setpoint is not none else 'None' }}",
        "mode_state_topic": topic + "/state",
        "mode_state_template": "{{ value_json.mode }}",
        "action_topic": topic + "/state",
        "action_template": "{{ value_json.action if value_json.action is not none else 'None' }}",
        "modes": ["heat"],
        "temperature_unit": "C",
        "precision": 0.1,
        "temp_step": 0.5,
        "min_temp": room["min_temp"],
        "max_temp": room["max_temp"],
        "optimistic": False,
        "retain": False,
        "qos": 0,
    }
    if config.allow_writes:
        payload["temperature_command_topic"] = topic + "/temperature/set"
    return (
        config.discovery_prefix
        + "/climate/danfoss_link_"
        + controller_id
        + "/room_"
        + str(room["id"])
        + "/config",
        payload,
    )


def parse_command(
    topic, payload, *, base, known_rooms, retained=False, duplicate=False
):
    if (
        retained
        or duplicate
        or not isinstance(payload, bytes)
        or not 1 <= len(payload) <= 32
    ):
        return None
    match = re.fullmatch(
        re.escape(base) + r"/room/(0|[1-9][0-9]{0,9})/temperature/set", topic
    )
    if match is None:
        return None
    room_id = int(match.group(1))
    if room_id not in known_rooms:
        return None
    try:
        text = payload.decode("ascii").strip()
        if not re.fullmatch(r"(?:[0-9]{1,2})(?:\.[0-9]{1,2})?", text):
            return None
        value = float(text)
        if not math.isfinite(value) or not 6 <= value <= 35:
            return None
    except (UnicodeError, ValueError):
        return None
    return room_id, value


def clean_world(world):
    if not isinstance(world, dict):
        raise ProtocolError("Invalid world response")
    rooms = world.get("allRooms")
    status = world.get("systemStatus")
    alarms = world.get("allUnresolvedAlarms")
    if (
        not isinstance(rooms, list)
        or any(not isinstance(r, dict) or type(r.get("id")) is not int for r in rooms)
        or not isinstance(status, dict)
        or not isinstance(status.get("currentState"), str)
        or not status["currentState"]
        or len(status["currentState"]) > 128
        or not status["currentState"].isprintable()
        or not isinstance(alarms, list)
    ):
        raise ProtocolError("Incomplete world response")
    safe_alarms = []
    for alarm in alarms:
        if not isinstance(alarm, dict):
            raise ProtocolError("Invalid alarm response")
        safe = {}
        for key in (
            "id",
            "isRead",
            "createdDate",
            "level",
            "room",
            "node",
            "titleTextId",
            "descriptionTextId",
        ):
            value = alarm.get(key)
            if type(value) in (int, bool):
                safe[key] = value
            elif isinstance(value, str):
                safe[key] = "".join(c for c in value if c.isprintable())[:512]
        safe_alarms.append(safe)
    cleaned = clean_rooms(rooms)
    for raw, room in zip(rooms, cleaned):
        state = raw.get("regulationState")
        room["action"] = {
            "Heating": "heating",
            "Deadband": "idle",
            "Cooling": "cooling",
        }.get(state)
    return {
        "rooms": cleaned,
        "current_state": status["currentState"],
        "alarms": safe_alarms,
    }


async def read_snapshot(transport):
    # Serialize through the same authenticated transport; no alarm acknowledgement.
    reply = await transport.exchange(read_request(1000000, "/api/world"))
    return clean_world(response_content(reply, 1000000))


async def cloud_operation(state, writes, command=None):
    async with asyncio.timeout(75):
        transport = await PairedControlTransport.connect(state, allow_writes=writes)
        try:
            client = DanfossClient(transport, allow_writes=writes)
            if command is not None:
                await client.set_temperature(*command)
            return await read_snapshot(transport)
        finally:
            await transport.close()


class MQTTBridge:
    def __init__(self, config, state, mqtt_client=None):
        self.config = config
        self.state = Path(state)
        _, _, peer_id, _ = load_trust(self.state)
        self.controller_id = hashlib.sha256(peer_id.encode()).hexdigest()[:16]
        self.base = config.prefix + "/" + self.controller_id
        self.connected = False
        self.cloud_ready = False
        self.epoch = 0
        self.rooms = {}
        self.queue = None
        self.loop = None
        self.refresh = None
        self.stop = None
        self.cloud_lock = None
        self.published_configs = {}
        self.force_discovery = True
        self.known_ids = set()
        self.discovery_record = self.state / "mqtt_discovery_rooms.json"
        if self.discovery_record.exists():
            info = self.discovery_record.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_mode & 0o077
                or info.st_size > 65535
            ):
                raise ValueError("Invalid discovery state file")
            record = json.loads(self.discovery_record.read_text())
            if record.get("controller_id") == self.controller_id:
                ids = record.get("room_ids", [])
                if (
                    not isinstance(ids, list)
                    or len(ids) > 1000
                    or any(type(i) is not int or i < 0 for i in ids)
                ):
                    raise ValueError("Invalid discovery state")
                self.known_ids = set(ids)
        self.client = mqtt_client
        if self.client is None:
            import paho.mqtt.client as mqtt

            self.client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id="danfoss-link-" + self.controller_id,
                clean_session=True,
                protocol=mqtt.MQTTv311,
            )
        self.client.disable_logger()
        if config.username:
            self.client.username_pw_set(config.username, config.password or None)
        elif config.password:
            raise ValueError("MQTT password requires username")
        if config.tls:
            ca = config.ca_file or "/etc/ssl/certs/ca-certificates.crt"
            self.client.tls_set(ca_certs=ca)
        self.client.will_set(self.base + "/availability", "offline", qos=1, retain=True)
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.max_queued_messages_set(64)
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_message = self.on_message

    def _notify(self, callback, *args):
        if self.loop is not None and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(callback, *args)

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            print("MQTT connection refused; credentials are not logged", flush=True)
            return
        self._notify(self._connected)

    def _connected(self):
        self.connected = True
        self.cloud_ready = False
        self.epoch += 1
        self.published_configs.clear()
        self.force_discovery = True
        self.client.publish(self.base + "/availability", "offline", qos=1, retain=True)
        self.client.subscribe(self.config.discovery_prefix + "/status", qos=0)
        if self.config.allow_writes:
            self.client.subscribe(self.base + "/room/+/temperature/set", qos=0)
        self.refresh.set()
        print("MQTT connected; awaiting verified cloud state", flush=True)

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        self._notify(self._disconnected)

    def _disconnected(self):
        self.connected = False
        self.cloud_ready = False
        self.epoch += 1
        print("MQTT disconnected; queued temperature commands invalidated", flush=True)

    def on_message(self, client, userdata, message):
        if (
            message.topic == self.config.discovery_prefix + "/status"
            and message.payload == b"online"
        ):
            self._notify(self._rediscover)
            return
        if not self.config.allow_writes:
            return
        command = parse_command(
            message.topic,
            message.payload,
            base=self.base,
            known_rooms=self.rooms,
            retained=message.retain,
            duplicate=message.dup,
        )
        if command is not None:
            self._notify(self._enqueue, command, self.epoch, time.monotonic())

    def _rediscover(self):
        self.force_discovery = True
        self.refresh.set()

    def _enqueue(self, command, epoch, received):
        if not self.connected or not self.cloud_ready or epoch != self.epoch:
            return
        try:
            self.queue.put_nowait((command, epoch, received))
        except asyncio.QueueFull:
            print("Temperature command rejected: queue full", flush=True)

    def publish_rooms(self, rooms, *, rediscover=False):
        if not self.connected:
            return
        current = {r["id"]: r for r in rooms}
        for room_id in self.known_ids - current.keys():
            topic = (
                self.config.discovery_prefix
                + "/climate/danfoss_link_"
                + self.controller_id
                + "/room_"
                + str(room_id)
                + "/config"
            )
            self.client.publish(topic, "", qos=1, retain=True)
            self.published_configs.pop(room_id, None)
            self.client.publish(
                self.base + "/room/" + str(room_id) + "/state", "", qos=1, retain=True
            )
        self.rooms = current
        if self.known_ids != set(current):
            record = {"controller_id": self.controller_id, "room_ids": sorted(current)}
            with tempfile.NamedTemporaryFile(
                mode="w", dir=self.state, delete=False
            ) as file:
                name = file.name
                os.fchmod(file.fileno(), 0o600)
                json.dump(record, file)
                file.flush()
                os.fsync(file.fileno())
            try:
                os.replace(name, self.discovery_record)
            finally:
                if os.path.exists(name):
                    os.unlink(name)
            self.known_ids = set(current)
        for room in rooms:
            topic, config = discovery(self.config, self.controller_id, room)
            text = json.dumps(config, separators=(",", ":"), allow_nan=False)
            if rediscover or self.published_configs.get(room["id"]) != text:
                self.client.publish(topic, text, qos=1, retain=True)
                self.published_configs[room["id"]] = text
            state = {
                "temperature": room["temperature"],
                "setpoint": room["setpoint"],
                "mode": "heat",
                "action": room.get("action"),
            }
            self.client.publish(
                self.base + "/room/" + str(room["id"]) + "/state",
                json.dumps(state, separators=(",", ":"), allow_nan=False),
                qos=1,
                retain=True,
            )
        self.cloud_ready = True
        self.client.publish(self.base + "/availability", "online", qos=1, retain=True)

    def publish_snapshot(self, snapshot, *, rediscover=False):
        if not self.connected:
            return
        shared = {
            "device": {
                "identifiers": ["danfoss_link_" + self.controller_id],
                "name": "Danfoss Link CC",
                "manufacturer": "Danfoss",
                "model": "Link CC",
            },
            "availability_topic": self.base + "/availability",
            "payload_available": "online",
            "payload_not_available": "offline",
            "qos": 1,
            "expire_after": max(180, self.config.poll_interval * 3),
        }
        entities = [
            (
                "sensor",
                "system_state",
                "Systemstatus",
                {"icon": "mdi:home-thermometer"},
                self.base + "/system/state",
                "{{ value_json.current_state }}",
            ),
            (
                "sensor",
                "alarm_count",
                "Antal alarmer",
                {"icon": "mdi:alert-circle-outline"},
                self.base + "/alarms/state",
                "{{ value_json.count }}",
            ),
            (
                "binary_sensor",
                "alarm",
                "Alarm",
                {
                    "device_class": "problem",
                    "payload_on": "ON",
                    "payload_off": "OFF",
                    "json_attributes_topic": self.base + "/alarms/attributes",
                },
                self.base + "/alarms/state",
                "{{ 'ON' if value_json.count > 0 else 'OFF' }}",
            ),
        ]
        for component, key, name, extra, state_topic, template in entities:
            config = dict(
                shared,
                name=name,
                unique_id="danfoss_link_" + self.controller_id + "_" + key,
                state_topic=state_topic,
                value_template=template,
                **extra,
            )
            text = json.dumps(config, separators=(",", ":"), allow_nan=False)
            if rediscover or self.published_configs.get(key) != text:
                topic = (
                    self.config.discovery_prefix
                    + "/"
                    + component
                    + "/danfoss_link_"
                    + self.controller_id
                    + "/"
                    + key
                    + "/config"
                )
                self.client.publish(topic, text, qos=1, retain=True)
                self.published_configs[key] = text
        self.client.publish(
            self.base + "/system/state",
            json.dumps({"current_state": snapshot["current_state"]}),
            qos=1,
            retain=True,
        )
        self.client.publish(
            self.base + "/alarms/attributes",
            json.dumps({"alarms": snapshot["alarms"]}, ensure_ascii=True),
            qos=1,
            retain=True,
        )
        self.client.publish(
            self.base + "/alarms/state",
            json.dumps({"count": len(snapshot["alarms"])}),
            qos=1,
            retain=True,
        )
        self.publish_rooms(snapshot["rooms"], rediscover=rediscover)

    def offline(self):
        self.cloud_ready = False
        if self.connected:
            return self.client.publish(
                self.base + "/availability", "offline", qos=1, retain=True
            )

    async def poll(self):
        while not self.stop.is_set():
            self.refresh.clear()
            if self.connected:
                try:
                    async with self.cloud_lock:
                        snapshot = await cloud_operation(
                            self.state, self.config.allow_writes
                        )
                        self.publish_snapshot(snapshot, rediscover=self.force_discovery)
                        if self.connected:
                            self.force_discovery = False
                    print(
                        "Cloud rooms refreshed; room_count="
                        + str(len(snapshot["rooms"])),
                        flush=True,
                    )
                except Exception:
                    self.offline()
                    print("Cloud read failed; entities marked unavailable", flush=True)
            try:
                await asyncio.wait_for(self.refresh.wait(), self.config.poll_interval)
            except asyncio.TimeoutError:
                pass

    async def command_worker(self):
        while not self.stop.is_set():
            command, epoch, received = await self.queue.get()
            try:
                async with self.cloud_lock:
                    if (
                        not self.connected
                        or not self.cloud_ready
                        or epoch != self.epoch
                        or time.monotonic() - received > 30
                    ):
                        print(
                            "Temperature command discarded: disconnected or expired",
                            flush=True,
                        )
                        continue
                    # Drop obsolete commands again immediately before applying the write.
                    async with asyncio.timeout(75):
                        transport = await PairedControlTransport.connect(
                            self.state, allow_writes=True
                        )
                        try:
                            if (
                                not self.connected
                                or epoch != self.epoch
                                or time.monotonic() - received > 30
                            ):
                                continue
                            client = DanfossClient(transport, allow_writes=True)
                            await client.set_temperature(*command)
                            self.publish_snapshot(await read_snapshot(transport))
                        finally:
                            await transport.close()
                    print(
                        "Temperature command applied and readback verified", flush=True
                    )
            except Exception:
                self.offline()
                self.refresh.set()
                print(
                    "Temperature result not verified; PATCH is not retried", flush=True
                )
            finally:
                self.queue.task_done()

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue(maxsize=16)
        self.refresh = asyncio.Event()
        self.stop = asyncio.Event()
        self.cloud_lock = asyncio.Lock()
        for sig in (signal.SIGINT, signal.SIGTERM):
            self.loop.add_signal_handler(sig, self.stop.set)
        self.client.connect_async(self.config.host, self.config.port, keepalive=30)
        self.client.loop_start()
        tasks = [
            asyncio.create_task(self.poll()),
            asyncio.create_task(self.command_worker()),
        ]
        try:
            await self.stop.wait()
        finally:
            publication = self.offline()
            if publication is not None:
                try:
                    await asyncio.to_thread(publication.wait_for_publish, 3)
                except Exception:
                    pass
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.client.disconnect()
            await asyncio.to_thread(self.client.loop_stop)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", type=Path, default=Path("/state/apk-profile"))
    args = parser.parse_args()
    try:
        asyncio.run(MQTTBridge(Settings.from_env(), args.state).run())
    except Exception:
        print(
            "MQTT bridge stopped; check configuration and paired state; details suppressed",
            flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
