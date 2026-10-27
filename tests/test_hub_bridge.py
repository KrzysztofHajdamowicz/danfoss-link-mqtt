import asyncio, contextlib, copy, io, json, os, tempfile, time, unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo
from hub_bridge import Command, HubBridge, parse_command
from hub_fixtures import WORLD, FakeCC
from hub_model import parse_world
from mqtt_bridge import Settings

BASE = "danfoss_link/cid"
TRUST = (None, None, "synthetic-peer", b"public-fixture")


class ParseCommandTests(unittest.TestCase):
    def parse(self, path, payload, **kwargs):
        return parse_command(BASE + "/" + path, payload, base=BASE, rooms={5}, onoff={3}, **kwargs)

    def test_valid_commands(self):
        self.assertEqual(self.parse("room/5/temperature/set", b"21.5"), Command("room", (5, "setpoint"), 21.5))
        self.assertEqual(self.parse("room/5/away_setpoint/set", b"17"), Command("room", (5, "away_setpoint"), 17.0))
        self.assertEqual(self.parse("hub/pause/set", b"ON"), Command("mode", "pause", True))
        self.assertEqual(self.parse("hub/athome/set", b"OFF"), Command("mode", "athome", False))
        self.assertEqual(self.parse("hub/pause_temperature/set", b"7.5"), Command("setting", "pause", 7.5))
        self.assertEqual(self.parse("onoff/3/set", b"ON"), Command("onoff", 3, True))
        command = self.parse("hub/vacation_end/set", b"2027-01-02T17:00:00+00:00")
        self.assertEqual(command, Command("vacation_time", "end", datetime(2027, 1, 2, 17, tzinfo=timezone.utc)))

    def test_rejected_commands(self):
        for path, payload in (
            ("room/6/temperature/set", b"21"),
            ("room/5/temperature/set", b"5"),
            ("room/5/temperature/set", b"nan"),
            ("room/5/temperature/set", b"21e0"),
            ("room/5/regulation/set", b"Schedule"),
            ("hub/pause/set", b"on"),
            ("hub/boost/set", b"ON"),
            ("hub/pause_temperature/set", b"11"),
            ("hub/vacation_temperature/set", b"4.5"),
            ("hub/vacation_end/set", b"2027-01-02T17:00:00"),
            ("hub/vacation_end/set", b"1999-01-02T17:00:00+00:00"),
            ("onoff/4/set", b"ON"),
            ("room/5/temperature/set", b"\xff"),
            ("room/5/temperature/set", b"2" * 65),
        ):
            with self.subTest(path=path, payload=payload):
                self.assertIsNone(self.parse(path, payload))
        self.assertIsNone(self.parse("hub/pause/set", b"ON", retained=True))
        self.assertIsNone(self.parse("hub/pause/set", b"ON", duplicate=True))
        self.assertIsNone(parse_command("other/hub/pause/set", b"ON", base=BASE, rooms=set(), onoff=set()))


def message(topic, payload, retain=False, dup=False):
    return SimpleNamespace(topic=topic, payload=payload, retain=retain, dup=dup)


class HubBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.state.chmod(0o700)
        self.sdk = Mock()
        self.sdk.subscribe.return_value = (0, 42)
        self.bridge = self.make_bridge()
        self.snapshot = parse_world(WORLD)

    def make_bridge(self, config=None):
        with patch("hub_bridge.load_trust", return_value=TRUST):
            bridge = HubBridge(config or Settings("broker"), self.state, self.sdk, tz=ZoneInfo("Europe/Warsaw"))
        bridge.loop = asyncio.get_running_loop()
        bridge.queue = asyncio.Queue(maxsize=16)
        bridge.refresh = asyncio.Event()
        bridge.stop = asyncio.Event()
        bridge.cloud_lock = asyncio.Lock()
        bridge.legacy_scanned = asyncio.Event()
        bridge.connected = True
        bridge.epoch = 1
        return bridge

    async def asyncTearDown(self):
        self.temp.cleanup()

    def published(self, suffix=""):
        return [(c.args[0], c.args[1]) for c in self.sdk.publish.call_args_list if c.args[0].endswith(suffix)]

    def last(self, topic):
        payload = [p for t, p in self.published() if t == topic][-1]
        return json.loads(payload) if payload.startswith("{") else payload

    async def run_worker(self, transport=None, side_effect=None):
        with patch("hub_bridge.HubTransport.connect", new_callable=AsyncMock, return_value=transport, side_effect=side_effect) as connect:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                task = asyncio.create_task(self.bridge.command_worker())
                await asyncio.wait_for(self.bridge.queue.join(), 2)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        return connect, output.getvalue()

    def enqueue(self, *commands, age=0):
        for command in commands:
            self.bridge.queue.put_nowait((command, self.bridge.epoch, time.monotonic() - age))

    async def test_publish_devices_states_and_availability(self):
        self.bridge.publish_snapshot(self.snapshot)
        topics = [t for t, _ in self.published("/config")]
        self.assertEqual(
            topics,
            [
                "homeassistant/device/danfoss_link_" + self.bridge.controller_id + "/config",
                "homeassistant/device/danfoss_link_" + self.bridge.controller_id + "_room_1/config",
                "homeassistant/device/danfoss_link_" + self.bridge.controller_id + "_room_5/config",
            ],
        )
        for call in self.sdk.publish.call_args_list:
            self.assertTrue(call.kwargs["retain"])
        base = self.bridge.base
        self.assertEqual(self.last(base + "/hub/state")["pause"], "OFF")
        self.assertEqual(self.last(base + "/room/5/state")["setpoint"], 20.0)
        self.assertEqual(self.last(base + "/room/5/attributes")["zone"], "Living")
        self.assertEqual(self.published()[-1], (base + "/availability", "online"))
        self.assertTrue(self.bridge.cloud_ready)
        self.sdk.publish.reset_mock()
        self.bridge.publish_snapshot(self.snapshot)
        self.assertEqual(self.published("/config"), [])
        self.bridge.publish_snapshot(self.snapshot, rediscover=True)
        self.assertEqual(len(self.published("/config")), 3)

    async def test_removed_room_cleaned_after_restart(self):
        self.bridge.publish_snapshot(self.snapshot)
        self.assertEqual(os.stat(self.state / "mqtt_discovery_hub.json").st_mode & 0o777, 0o600)
        self.sdk.reset_mock()
        restarted = self.make_bridge()
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["rooms"] = snapshot["rooms"][:1]
        restarted.publish_snapshot(snapshot)
        room_topic = "homeassistant/device/danfoss_link_" + restarted.controller_id + "_room_5/config"
        self.assertIn((room_topic, ""), self.published())
        self.assertIn((restarted.base + "/room/5/state", ""), self.published())

    async def test_flat_discovery_migrated_in_documented_order(self):
        prefix = "homeassistant/climate/danfoss_link_" + self.bridge.controller_id
        legacy = prefix + "/room_5/config"
        self.bridge.on_message(None, None, message(legacy, b'{"name":"K"}', retain=True))
        self.bridge.on_message(None, None, message(prefix + "/room_1/config", b'{"migrate_discovery":true}', retain=True))
        await asyncio.sleep(0)
        self.assertEqual(self.bridge.legacy, {legacy})
        self.bridge.publish_snapshot(self.snapshot)
        sequence = [(t, p) for t, p in self.published() if t == legacy or t.endswith("/config")]
        self.assertEqual(sequence[0], (legacy, '{"migrate_discovery":true}'))
        self.assertTrue(sequence[1][0].startswith("homeassistant/device/"))
        self.assertEqual(sequence[-1], (legacy, ""))
        self.assertEqual(self.bridge.legacy, set())

    async def test_removed_hub_component_uses_platform_only_entry(self):
        snapshot = copy.deepcopy(self.snapshot)
        snapshot["onoff"] = [{"id": 3, "name": "Pump", "on": False}]
        self.bridge.publish_snapshot(snapshot)
        self.assertEqual(self.last(self.bridge.base + "/onoff/3/state"), "OFF")
        hub = "homeassistant/device/danfoss_link_" + self.bridge.controller_id + "/config"
        self.bridge.publish_snapshot(self.snapshot)
        self.assertEqual(self.last(hub)["components"]["onoff_3"], {"platform": "switch"})
        self.bridge.publish_snapshot(self.snapshot)
        self.assertNotIn("onoff_3", self.last(hub)["components"])

    async def test_commands_coalesced_into_one_cloud_session(self):
        self.bridge.publish_snapshot(self.snapshot)
        cc = FakeCC()
        cc.close = AsyncMock()
        self.enqueue(
            Command("room", (5, "setpoint"), 19.0),
            Command("room", (5, "setpoint"), 19.5),
            Command("room", (5, "athome_setpoint"), 22.0),
            Command("room", (5, "setpoint"), 20.5),
        )
        connect, _ = await self.run_worker(cc)
        self.assertEqual(connect.await_count, 1)
        self.assertEqual(
            [w[2] for w in cc.writes],
            [{"id": 5, "regulation": {"atHomeSetpoint": 22.0}}, {"id": 5, "setpoint": {"temperature": 20.5}}],
        )
        cc.close.assert_awaited_once()
        self.assertEqual(self.last(self.bridge.base + "/room/5/state")["setpoint"], 20.5)
        self.assertEqual(self.last(self.bridge.base + "/hub/last_command")["result"], "applied")

    async def test_expired_or_disconnected_commands_never_connect(self):
        self.bridge.publish_snapshot(self.snapshot)
        self.enqueue(Command("mode", "pause", True), age=31)
        connect, _ = await self.run_worker(FakeCC())
        connect.assert_not_called()
        self.assertEqual(self.last(self.bridge.base + "/hub/last_command")["result"], "expired")
        self.bridge._enqueue(Command("mode", "pause", True), 1, time.monotonic())
        with contextlib.redirect_stdout(io.StringIO()):
            self.bridge._disconnected()
        connect, _ = await self.run_worker(FakeCC())
        connect.assert_not_called()

    async def test_rejected_command_reported_and_stays_online(self):
        self.bridge.publish_snapshot(self.snapshot)
        cc = FakeCC()
        cc.close = AsyncMock()
        cc.mode("Pause")["state"] = "Active"
        self.enqueue(Command("room", (5, "setpoint"), 21.0))
        await self.run_worker(cc)
        report = self.last(self.bridge.base + "/hub/last_command")
        self.assertEqual(report["result"], "rejected")
        self.assertIn("Pause", report["message"])
        self.assertEqual(cc.writes, [])
        self.assertTrue(self.bridge.cloud_ready)

    async def test_uncertain_write_not_retried_and_secret_not_logged(self):
        self.bridge.publish_snapshot(self.snapshot)
        cc = FakeCC(fail_write=True)
        cc.close = AsyncMock()
        self.enqueue(Command("mode", "pause", True))
        _, output = await self.run_worker(cc)
        self.assertEqual(len(cc.writes), 1)
        self.assertNotIn("synthetic-secret", output)
        self.assertNotIn("synthetic-secret", json.dumps(self.published()))
        self.assertFalse(self.bridge.cloud_ready)
        self.assertEqual(self.last(self.bridge.base + "/hub/last_command")["result"], "unverified")
        self.assertEqual(self.last(self.bridge.base + "/availability"), "offline")

    async def test_holiday_window_stored_then_sent_in_cc_local_time(self):
        self.bridge.publish_snapshot(self.snapshot)
        end = datetime(2099, 1, 2, 17, tzinfo=timezone.utc)
        self.enqueue(Command("vacation_time", "end", end))
        connect, _ = await self.run_worker(FakeCC())
        connect.assert_not_called()
        record = json.loads((self.state / "mqtt_discovery_hub.json").read_text())
        self.assertEqual(record["pending"]["end"], end.isoformat())
        self.assertEqual(self.last(self.bridge.base + "/hub/state")["vacation_end"], end.isoformat())
        cc = FakeCC()
        cc.close = AsyncMock()
        self.enqueue(Command("mode", "vacation", True))
        await self.run_worker(cc)
        content = cc.writes[0][2]
        self.assertEqual(content["endTime"], "2099-01-02T18:00:00")
        self.assertLessEqual(
            abs((datetime.fromisoformat(content["startTime"]) - datetime.now()).total_seconds()),
            7200,
        )
        self.assertEqual(self.last(self.bridge.base + "/hub/state")["vacation"], "ON")

    async def test_holiday_without_end_rejected_locally(self):
        self.bridge.publish_snapshot(self.snapshot)
        cc = FakeCC()
        cc.close = AsyncMock()
        self.enqueue(Command("mode", "vacation", True))
        await self.run_worker(cc)
        self.assertEqual(cc.writes, [])
        self.assertEqual(self.last(self.bridge.base + "/hub/last_command")["result"], "rejected")

    async def test_home_assistant_restart_triggers_rediscovery(self):
        with patch("hub_bridge.random.uniform", return_value=0):
            self.bridge.on_message(None, None, message("homeassistant/status", b"online"))
            await asyncio.sleep(0.01)
        self.assertTrue(self.bridge.refresh.is_set())
        self.assertTrue(self.bridge.force_discovery)

    async def test_readonly_bridge_ignores_commands(self):
        bridge = self.make_bridge(Settings("broker", allow_writes=False))
        bridge.publish_snapshot(self.snapshot)
        bridge.on_message(None, None, message(bridge.base + "/hub/pause/set", b"ON"))
        await asyncio.sleep(0)
        self.assertTrue(bridge.queue.empty())


if __name__ == "__main__":
    unittest.main()
