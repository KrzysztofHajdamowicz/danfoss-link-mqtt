"""Danfoss Link hub and per-room devices via MQTT device discovery, with verified commands."""

import argparse, asyncio, hashlib, json, math, os, random, re, signal, ssl, stat, tempfile, time
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from danfoss_mdg.readonly_transport import load_trust
from hub_client import HubClient, HubPrecondition, HubUnverified
from hub_model import (
    aware_to_cc,
    hub_discovery,
    hub_state,
    room_attributes,
    room_discovery,
    room_state,
)
from hub_transport import HubRejected, HubTransport
from mqtt_bridge import Settings

Command = namedtuple("Command", "kind target value")
NUMBER = re.compile(r"[0-9]{1,2}(?:\.[0-9]{1,2})?")
ID = r"(0|[1-9][0-9]{0,9})"
SWITCH = {"ON": True, "OFF": False}
ROOM_FIELDS = {
    "temperature": "setpoint",
    "athome_setpoint": "athome_setpoint",
    "away_setpoint": "away_setpoint",
}
SETTING_LIMITS = {"pause_temperature": (4, 10), "vacation_temperature": (5, 35)}
LEGACY_COMPONENTS = ("climate", "sensor", "binary_sensor")
RECORD = "mqtt_discovery_hub.json"


def _number(text, low, high):
    if not NUMBER.fullmatch(text):
        return None
    value = float(text)
    return value if math.isfinite(value) and low <= value <= high else None


def _moment(text):
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None or not 2000 <= moment.year <= 2099:
        return None
    return moment.astimezone(timezone.utc).replace(microsecond=0)


def parse_command(
    topic, payload, *, base, rooms, onoff, retained=False, duplicate=False
):
    """Map one MQTT command to a Command, or None; bounds match the client's."""
    if (
        retained
        or duplicate
        or not isinstance(payload, bytes)
        or not 1 <= len(payload) <= 64
        or not topic.startswith(base + "/")
    ):
        return None
    try:
        text = payload.decode("ascii").strip()
    except UnicodeError:
        return None
    path = topic[len(base) + 1 :]
    if match := re.fullmatch(
        r"room/" + ID + r"/(temperature|athome_setpoint|away_setpoint)/set", path
    ):
        room_id = int(match.group(1))
        value = _number(text, 6, 35)
        if room_id not in rooms or value is None:
            return None
        return Command("room", (room_id, ROOM_FIELDS[match.group(2)]), value)
    if match := re.fullmatch(r"hub/(pause|vacation|athome)/set", path):
        on = SWITCH.get(text)
        return None if on is None else Command("mode", match.group(1), on)
    if match := re.fullmatch(r"hub/vacation_(start|end)/set", path):
        moment = _moment(text)
        return None if moment is None else Command("vacation_time", match.group(1), moment)
    if match := re.fullmatch(r"hub/((?:pause|vacation)_temperature)/set", path):
        value = _number(text, *SETTING_LIMITS[match.group(1)])
        kind = match.group(1).removesuffix("_temperature")
        return None if value is None else Command("setting", kind, value)
    if match := re.fullmatch(r"onoff/" + ID + r"/set", path):
        device_id, on = int(match.group(1)), SWITCH.get(text)
        if device_id not in onoff or on is None:
            return None
        return Command("onoff", device_id, on)
    return None


def describe(command):
    if command.kind == "room":
        return "room " + str(command.target[0]) + " " + command.target[1]
    if command.kind == "vacation_time":
        return "holiday " + command.target
    return command.kind + " " + str(command.target)


async def cloud_session(state, operation, *, writes=False, timeout=75):
    async with asyncio.timeout(timeout):
        transport = await HubTransport.connect(state)
        try:
            return await operation(HubClient(transport, allow_writes=writes))
        finally:
            await transport.close()


def private_json(path, record):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        name = file.name
        os.fchmod(file.fileno(), 0o600)
        json.dump(record, file)
        file.flush()
        os.fsync(file.fileno())
    try:
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def timezone_from_env():
    explicit = os.environ.get("DANFOSS_TIMEZONE", "").strip()
    try:
        if explicit:
            return ZoneInfo(explicit)
        if os.environ.get("TZ", "").strip():
            return ZoneInfo(os.environ["TZ"].strip())
    except (ZoneInfoNotFoundError, ValueError):
        if explicit:
            raise ValueError("Invalid DANFOSS_TIMEZONE") from None
        print("TZ is not an IANA zone; using UTC for holiday times", flush=True)
    return timezone.utc


class HubBridge:
    def __init__(
        self, config, state, mqtt_client=None, *, tz=timezone.utc, suggest_areas=True
    ):
        self.config = config
        self.state = Path(state)
        _, _, peer_id, _ = load_trust(self.state)
        self.controller_id = hashlib.sha256(peer_id.encode()).hexdigest()[:16]
        self.base = config.prefix + "/" + self.controller_id
        self.tz = tz
        self.suggest_areas = suggest_areas
        self.connected = False
        self.cloud_ready = False
        self.epoch = 0
        self.snapshot = None
        self.rooms = set()
        self.onoff = set()
        self.queue = self.loop = self.refresh = self.stop = None
        self.cloud_lock = self.legacy_scanned = None
        self.published = {}
        self.hub_platforms = {}
        self.force_discovery = True
        self.legacy = set()
        self.legacy_mid = None
        self.record = self.state / RECORD
        self.known_rooms, self.pending = self._load_record()
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
            self.client.tls_set(ca_certs=config.ca_file or "/etc/ssl/certs/ca-certificates.crt")
        self.client.will_set(self.base + "/availability", "offline", qos=1, retain=True)
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.max_queued_messages_set(256)
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_subscribe = self.on_subscribe
        self.client.on_message = self.on_message

    def _load_record(self):
        pending = {"start": None, "end": None}
        if not self.record.exists():
            return set(), pending
        info = self.record.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > 65535
        ):
            raise ValueError("Invalid discovery state file")
        record = json.loads(self.record.read_text())
        if not isinstance(record, dict) or record.get("controller_id") != self.controller_id:
            return set(), pending
        ids = record.get("room_ids", [])
        if (
            not isinstance(ids, list)
            or len(ids) > 1000
            or any(type(i) is not int or i < 0 for i in ids)
        ):
            raise ValueError("Invalid discovery state")
        saved = record.get("pending")
        for key in pending:
            value = saved.get(key) if isinstance(saved, dict) else None
            pending[key] = _moment(value) if isinstance(value, str) else None
        return set(ids), pending

    def _save_record(self):
        private_json(
            self.record,
            {
                "controller_id": self.controller_id,
                "room_ids": sorted(self.known_rooms),
                "pending": {
                    k: v.isoformat() if v else None for k, v in self.pending.items()
                },
            },
        )

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
        self.published.clear()
        self.force_discovery = True
        self.legacy.clear()
        self.legacy_scanned.clear()
        prefix = self.config.discovery_prefix
        self.client.publish(self.base + "/availability", "offline", qos=1, retain=True)
        self.client.subscribe(prefix + "/status", qos=0)
        if self.config.allow_writes:
            self.client.subscribe(
                [
                    (self.base + "/room/+/+/set", 0),
                    (self.base + "/hub/+/set", 0),
                    (self.base + "/onoff/+/set", 0),
                ]
            )
        # Retained flat-bridge discovery is replayed after SUBACK and migrated once.
        _, self.legacy_mid = self.client.subscribe(
            [
                (prefix + "/" + c + "/danfoss_link_" + self.controller_id + "/+/config", 0)
                for c in LEGACY_COMPONENTS
            ]
        )
        self.refresh.set()
        print("MQTT connected; awaiting verified cloud state", flush=True)

    def on_subscribe(self, client, userdata, mid, reason_codes, properties):
        if mid == self.legacy_mid:
            self._notify(self.loop.call_later, 1.0, self.legacy_scanned.set)

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        self._notify(self._disconnected)

    def _disconnected(self):
        self.connected = False
        self.cloud_ready = False
        self.epoch += 1
        print("MQTT disconnected; queued commands invalidated", flush=True)

    def on_message(self, client, userdata, message):
        prefix = self.config.discovery_prefix
        if message.topic == prefix + "/status":
            if message.payload == b"online":
                self._notify(self._rediscover)
            return
        if message.topic.startswith(prefix + "/"):
            if (
                message.retain
                and message.payload
                and b"migrate_discovery" not in message.payload
            ):
                self._notify(self.legacy.add, message.topic)
            return
        if not self.config.allow_writes:
            return
        command = parse_command(
            message.topic,
            message.payload,
            base=self.base,
            rooms=self.rooms,
            onoff=self.onoff,
            retained=message.retain,
            duplicate=message.dup,
        )
        if command is not None:
            self._notify(self._enqueue, command, self.epoch, time.monotonic())

    def _rediscover(self):
        # Spread rediscovery after a Home Assistant restart, as its docs recommend.
        self.loop.call_later(random.uniform(1, 5), self._force_refresh)

    def _force_refresh(self):
        self.force_discovery = True
        self.refresh.set()

    def _enqueue(self, command, epoch, received):
        if not self.connected or not self.cloud_ready or epoch != self.epoch:
            return
        try:
            self.queue.put_nowait((command, epoch, received))
        except asyncio.QueueFull:
            print("Command rejected: queue full", flush=True)

    def _send(self, topic, payload):
        if not isinstance(payload, str):
            payload = json.dumps(payload, separators=(",", ":"), allow_nan=False)
        return self.client.publish(topic, payload, qos=1, retain=True)

    def _discover(self, topic, payload, rediscover):
        text = json.dumps(payload, separators=(",", ":"), allow_nan=False)
        if rediscover or self.published.get(topic) != text:
            self._send(topic, text)
            self.published[topic] = text

    def _room_topic(self, room_id):
        return (
            self.config.discovery_prefix
            + "/device/danfoss_link_"
            + self.controller_id
            + "_room_"
            + str(room_id)
            + "/config"
        )

    def report(self, result, command, message=None):
        self._send(
            self.base + "/hub/last_command",
            {
                "result": result,
                "command": describe(command),
                "message": message,
                "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        )

    def publish_snapshot(self, snapshot, *, rediscover=False):
        if not self.connected:
            return
        self.snapshot = snapshot
        rooms = {r["id"]: r for r in snapshot["rooms"]}
        migrating = sorted(self.legacy)
        for topic in migrating:
            self._send(topic, {"migrate_discovery": True})
        for room_id in self.known_rooms - rooms.keys():
            self._send(self._room_topic(room_id), "")
            self.published.pop(self._room_topic(room_id), None)
            for suffix in ("/state", "/attributes"):
                self._send(self.base + "/room/" + str(room_id) + suffix, "")
        topic, payload = hub_discovery(self.config, self.controller_id, snapshot)
        components = payload["components"]
        for key, platform in self.hub_platforms.items():
            # Device discovery removes a component via a platform-only entry first.
            components.setdefault(key, {"platform": platform})
        self._discover(topic, payload, rediscover)
        self.hub_platforms = {
            k: c["platform"] for k, c in components.items() if len(c) > 1
        }
        for room in snapshot["rooms"]:
            self._discover(
                *room_discovery(
                    self.config,
                    self.controller_id,
                    room,
                    suggest_area=self.suggest_areas,
                ),
                rediscover,
            )
        if self.known_rooms != rooms.keys():
            self.known_rooms = set(rooms)
            self._save_record()
        for topic in migrating:
            self._send(topic, "")
            self.legacy.discard(topic)
        if migrating:
            print("Migrated flat discovery; entity_count=" + str(len(migrating)), flush=True)
        self._send(self.base + "/system/state", {"current_state": snapshot["current_state"]})
        self._send(self.base + "/alarms/attributes", {"alarms": snapshot["alarms"]})
        self._send(self.base + "/alarms/state", {"count": len(snapshot["alarms"])})
        self._send(self.base + "/hub/state", hub_state(snapshot, self.tz, self.pending))
        for room in snapshot["rooms"]:
            topic = self.base + "/room/" + str(room["id"])
            self._send(topic + "/state", room_state(room))
            self._send(topic + "/attributes", room_attributes(room))
        for device in snapshot["onoff"]:
            self._send(
                self.base + "/onoff/" + str(device["id"]) + "/state",
                {True: "ON", False: "OFF"}.get(device["on"], "None"),
            )
        self.rooms = set(rooms)
        self.onoff = {d["id"] for d in snapshot["onoff"]}
        self.cloud_ready = True
        self._send(self.base + "/availability", "online")

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
                    await asyncio.wait_for(self.legacy_scanned.wait(), 10)
                except asyncio.TimeoutError:
                    pass
                try:
                    async with self.cloud_lock:
                        snapshot = await cloud_session(
                            self.state, lambda client: client.snapshot()
                        )
                        self.publish_snapshot(snapshot, rediscover=self.force_discovery)
                        if self.connected:
                            self.force_discovery = False
                    print(
                        "Cloud state refreshed; room_count=" + str(len(snapshot["rooms"])),
                        flush=True,
                    )
                except Exception:
                    self.offline()
                    print("Cloud read failed; entities marked unavailable", flush=True)
            try:
                await asyncio.wait_for(self.refresh.wait(), self.config.poll_interval)
            except asyncio.TimeoutError:
                pass

    def _holiday_window(self):
        state = hub_state(self.snapshot, self.tz, self.pending)
        start, end = (
            datetime.fromisoformat(state[k]) if state[k] else None
            for k in ("vacation_start", "vacation_end")
        )
        now = datetime.now(timezone.utc).replace(microsecond=0)
        if end is None:
            raise HubPrecondition("Set the holiday end time first")
        if start is None or start < now:
            start = now
        if end <= start:
            raise HubPrecondition("Holiday end must be in the future and after its start")
        return aware_to_cc(start, self.tz), aware_to_cc(end, self.tz)

    async def _apply(self, client, command):
        if command.kind == "room":
            return await client.set_room(*command.target, command.value)
        if command.kind == "setting":
            return await client.set_temperature_setting(command.target, command.value)
        if command.kind == "onoff":
            return await client.set_onoff(command.target, command.value)
        if command.target == "vacation" and command.value:
            return await client.set_mode("vacation", True, *self._holiday_window())
        return await client.set_mode(command.target, command.value)

    def _valid(self, epoch, received):
        return (
            self.connected
            and self.cloud_ready
            and epoch == self.epoch
            and time.monotonic() - received <= 30
        )

    async def apply_batch(self, batch):
        latest = {}
        for command, epoch, received in batch:
            if not self._valid(epoch, received):
                self.report("expired", command, "Disconnected or older than 30 s")
                print("Command discarded: disconnected or expired", flush=True)
                continue
            # Coalesce slider bursts: only the newest value per entity is written.
            latest.pop((command.kind, command.target), None)
            latest[(command.kind, command.target)] = (command, epoch, received)
        cloud = []
        vacation = (self.snapshot["modes"]["vacation"] or {}) if self.snapshot else {}
        for command, epoch, received in latest.values():
            if command.kind != "vacation_time":
                cloud.append((command, epoch, received))
                continue
            self.pending[command.target] = command.value
            self._save_record()
            self.report("applied", command, "Stored; sent when Holiday is switched on")
            if vacation.get("active") and not any(
                c.kind == "mode" and c.target == "vacation" for c, _, _ in cloud
            ):
                cloud.insert(0, (Command("mode", "vacation", True), epoch, received))
        if self.snapshot is not None:
            self._send(self.base + "/hub/state", hub_state(self.snapshot, self.tz, self.pending))
        if not cloud:
            return
        current = cloud[0][0]

        async def operation(client):
            nonlocal current
            snapshot = None
            for command, epoch, received in cloud:
                current = command
                if not self._valid(epoch, received):
                    self.report("expired", command, "Disconnected or older than 30 s")
                    continue
                try:
                    snapshot = await self._apply(client, command)
                    self.report("applied", command)
                    print("Command applied and readback verified", flush=True)
                except HubRejected as error:
                    self.report("rejected", command, "; ".join(error.messages) or None)
                    print("Command rejected; status=" + str(error.status), flush=True)
            return snapshot or await client.snapshot()

        try:
            snapshot = await cloud_session(
                self.state, operation, writes=True, timeout=75 + 45 * len(cloud)
            )
        except Exception as error:
            # Never retried: an uncertain write must be checked on the CC first.
            result = "unverified" if isinstance(error, HubUnverified) else "failed"
            self.report(result, current, "Not retried; check the CC")
            raise
        self.publish_snapshot(snapshot)

    async def command_worker(self):
        while not self.stop.is_set():
            batch = [await self.queue.get()]
            while not self.queue.empty():
                batch.append(self.queue.get_nowait())
            try:
                async with self.cloud_lock:
                    await self.apply_batch(batch)
            except Exception:
                self.offline()
                self.refresh.set()
                print("Command result not verified; writes are not retried", flush=True)
            finally:
                for _ in batch:
                    self.queue.task_done()

    async def run(self):
        self.loop = asyncio.get_running_loop()
        self.queue = asyncio.Queue(maxsize=16)
        self.refresh = asyncio.Event()
        self.stop = asyncio.Event()
        self.cloud_lock = asyncio.Lock()
        self.legacy_scanned = asyncio.Event()
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
    if not ssl.OPENSSL_VERSION.startswith("OpenSSL 3.5.9 "):
        print("Run inside the isolated private MDG TLS runtime", flush=True)
        return 1
    code_file = os.environ.get("DANFOSS_PAIRING_CODE_FILE")
    if code_file and not (args.state / "paired_cc.json").exists():
        from pair_cc import pair_from_file

        pair_from_file(args.state, code_file)
        print("Paired; the pairing code file is no longer needed", flush=True)
    try:
        bridge = HubBridge(
            Settings.from_env(),
            args.state,
            tz=timezone_from_env(),
            suggest_areas=(os.environ.get("DANFOSS_SUGGEST_AREAS") or "true").lower()
            in ("1", "true", "yes"),
        )
        asyncio.run(bridge.run())
    except Exception:
        print(
            "Hub bridge stopped; check configuration and paired state; details suppressed",
            flush=True,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
