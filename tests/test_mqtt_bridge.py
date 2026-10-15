import asyncio, contextlib, io, json, os, tempfile, time, unittest
from pathlib import Path
from unittest.mock import Mock, patch, AsyncMock
from mqtt_bridge import Settings, discovery, parse_command, MQTTBridge
from rooms import clean_rooms

ROOM = {
    "id": 5,
    "name": "Køkken",
    "temperature": 20.4,
    "setpoint": 10.0,
    "min_temp": 6,
    "max_temp": 35,
}


class MQTTSettingsTests(unittest.TestCase):
    def test_only_host_required(self):
        with patch.dict(os.environ, {"MQTT_HOST": "synthetic-broker"}, clear=True):
            config = Settings.from_env()
        self.assertEqual(config.port, 1883)
        self.assertEqual(config.username, "")
        self.assertEqual(config.password, "")
        self.assertFalse(config.tls)
        self.assertTrue(config.allow_writes)

    def test_empty_optional_variables_use_defaults(self):
        env = {
            key: ""
            for key in (
                "MQTT_PORT",
                "MQTT_USERNAME",
                "MQTT_PASSWORD",
                "MQTT_TLS",
                "MQTT_TOPIC_PREFIX",
                "MQTT_DISCOVERY_PREFIX",
                "DANFOSS_POLL_INTERVAL",
                "DANFOSS_ALLOW_WRITES",
            )
        }
        env["MQTT_HOST"] = "synthetic-broker"
        with patch.dict(os.environ, env, clear=True):
            config = Settings.from_env()
        self.assertEqual(config, Settings("synthetic-broker"))

    def test_optional_auth_tls_and_password_not_repr(self):
        with patch.dict(
            os.environ,
            {
                "MQTT_HOST": "broker",
                "MQTT_TLS": "true",
                "MQTT_USERNAME": "synthetic",
                "MQTT_PASSWORD": "synthetic-secret",
            },
            clear=True,
        ):
            config = Settings.from_env()
        self.assertEqual(config.port, 8883)
        self.assertNotIn("synthetic-secret", repr(config))

    def test_topic_wildcards_and_missing_host_rejected(self):
        for env in (
            {},
            {"MQTT_HOST": "broker", "MQTT_TOPIC_PREFIX": "unsafe/#"},
            {"MQTT_HOST": "broker", "MQTT_PORT": "0"},
        ):
            with patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError):
                Settings.from_env()

    def test_names_stable_id_and_nonoptimistic_commands(self):
        config = Settings("broker")
        topic, data = discovery(config, "controller", ROOM)
        _, renamed = discovery(config, "controller", dict(ROOM, name="Bryggers"))
        self.assertEqual(data["name"], "Køkken")
        self.assertEqual(data["unique_id"], renamed["unique_id"])
        self.assertFalse(data["retain"])
        self.assertFalse(data["optimistic"])
        self.assertEqual(data["qos"], 0)
        self.assertIn("/room/5/temperature/set", data["temperature_command_topic"])

    def test_readonly_discovery_has_no_command_topic(self):
        _, data = discovery(Settings("broker", allow_writes=False), "controller", ROOM)
        self.assertNotIn("temperature_command_topic", data)

    def test_retained_duplicate_unknown_and_bad_commands_rejected(self):
        base = "danfoss_link/controller"
        topic = base + "/room/5/temperature/set"
        kwargs = {"base": base, "known_rooms": {5: ROOM}}
        self.assertEqual(parse_command(topic, b"10", **kwargs), (5, 10.0))
        self.assertIsNone(parse_command(topic, b"10", retained=True, **kwargs))
        self.assertIsNone(parse_command(topic, b"10", duplicate=True, **kwargs))
        for payload in (
            b"nan",
            b"inf",
            b"true",
            b"5",
            b"36",
            b'{"temperature":10}',
            b"10" * 40,
            b"\xff",
        ):
            self.assertIsNone(parse_command(topic, payload, **kwargs))
        self.assertIsNone(
            parse_command(base + "/room/6/temperature/set", b"10", **kwargs)
        )


class MQTTBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        self.sdk = Mock()
        with patch(
            "mqtt_bridge.load_trust",
            return_value=(None, None, "synthetic-peer", b"public-fixture"),
        ):
            self.bridge = MQTTBridge(Settings("broker"), self.state, self.sdk)
        b = self.bridge
        b.loop = asyncio.get_running_loop()
        b.queue = asyncio.Queue(maxsize=16)
        b.refresh = asyncio.Event()
        b.stop = asyncio.Event()
        b.cloud_lock = asyncio.Lock()
        b.connected = True
        b.cloud_ready = True
        b.epoch = 1

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def test_sdk_auth_optional_will_and_state_retained(self):
        self.sdk.username_pw_set.assert_not_called()
        self.sdk.will_set.assert_called_with(
            self.bridge.base + "/availability", "offline", qos=1, retain=True
        )
        self.bridge.publish_rooms([ROOM])
        for call in self.sdk.publish.call_args_list:
            self.assertTrue(call.kwargs["retain"])
            self.assertNotIn("/temperature/set", call.args[0])
        configs = [
            call
            for call in self.sdk.publish.call_args_list
            if call.args[0].endswith("/config")
        ]
        self.assertEqual(json.loads(configs[0].args[1])["name"], "Køkken")

    async def test_removed_room_discovery_cleaned_after_restart(self):
        self.bridge.publish_rooms([ROOM])
        self.sdk.reset_mock()
        with patch(
            "mqtt_bridge.load_trust",
            return_value=(None, None, "synthetic-peer", b"public-fixture"),
        ):
            restarted = MQTTBridge(Settings("broker"), self.state, self.sdk)
        restarted.connected = True
        restarted.publish_rooms([])
        deleted = [
            c
            for c in self.sdk.publish.call_args_list
            if c.args[0].endswith("/config") and c.args[1] == ""
        ]
        self.assertEqual(len(deleted), 1)

    async def test_disconnect_invalidates_queued_command(self):
        self.bridge._enqueue((5, 10.0), 1, time.monotonic())
        with contextlib.redirect_stdout(io.StringIO()):
            self.bridge._disconnected()
        with patch(
            "mqtt_bridge.PairedControlTransport.connect", new_callable=AsyncMock
        ) as connect:
            with contextlib.redirect_stdout(io.StringIO()):
                task = asyncio.create_task(self.bridge.command_worker())
                await asyncio.wait_for(self.bridge.queue.join(), 1)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            connect.assert_not_called()

    async def test_expired_command_never_connects_cloud(self):
        self.bridge.queue.put_nowait(((5, 10.0), 1, time.monotonic() - 31))
        with patch(
            "mqtt_bridge.PairedControlTransport.connect", new_callable=AsyncMock
        ) as connect:
            with contextlib.redirect_stdout(io.StringIO()):
                task = asyncio.create_task(self.bridge.command_worker())
                await asyncio.wait_for(self.bridge.queue.join(), 1)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            connect.assert_not_called()

    async def test_failed_write_not_requeued_and_secret_not_logged(self):
        self.bridge.queue.put_nowait(((5, 10.0), 1, time.monotonic()))
        output = io.StringIO()
        with patch(
            "mqtt_bridge.PairedControlTransport.connect",
            new_callable=AsyncMock,
            side_effect=OSError("synthetic-secret"),
        ) as connect:
            with contextlib.redirect_stdout(output):
                task = asyncio.create_task(self.bridge.command_worker())
                await asyncio.wait_for(self.bridge.queue.join(), 1)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(connect.await_count, 1)
        self.assertNotIn("synthetic-secret", output.getvalue())
        self.assertFalse(self.bridge.cloud_ready)
