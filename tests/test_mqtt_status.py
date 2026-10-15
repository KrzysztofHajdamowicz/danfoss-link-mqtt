import copy, json, unittest, asyncio, contextlib, io
from unittest.mock import patch, AsyncMock
from mqtt_bridge import clean_world, read_snapshot, discovery, Settings
from danfoss_readonly import ProtocolError
import test_mqtt_bridge as fixtures

WORLD = {
    "allRooms": [
        {
            "id": 5,
            "name": "Køkken",
            "ambientTemperature": 20,
            "setpoint": {"temperature": 19},
            "regulationState": "Heating",
        }
    ],
    "systemStatus": {"currentState": "Asleep"},
    "allUnresolvedAlarms": [],
}


class StatusParsingTests(unittest.TestCase):
    def test_action_mapping_and_unknown_resets(self):
        for state, action in [
            ("Heating", "heating"),
            ("Deadband", "idle"),
            ("Cooling", "cooling"),
            ("ForecastHeating", None),
            ("Invalid", None),
            (None, None),
        ]:
            world = copy.deepcopy(WORLD)
            world["allRooms"][0]["regulationState"] = state
            self.assertEqual(clean_world(world)["rooms"][0]["action"], action)

    def test_incomplete_alarm_or_status_never_becomes_zero(self):
        for key, value in [
            ("allUnresolvedAlarms", None),
            ("allUnresolvedAlarms", [None]),
            ("systemStatus", {}),
            ("allRooms", None),
        ]:
            world = copy.deepcopy(WORLD)
            world[key] = value
            with self.assertRaises(ProtocolError):
                clean_world(world)
        world = copy.deepcopy(WORLD)
        del world["allUnresolvedAlarms"]
        with self.assertRaises(ProtocolError):
            clean_world(world)

    def test_alarm_fields_allowlisted(self):
        world = copy.deepcopy(WORLD)
        world["allUnresolvedAlarms"] = [
            {
                "id": 8,
                "node": "Thermostat 2",
                "room": "Køkken",
                "level": "Warning",
                "titleTextId": "BATTERY_LOW",
                "createdDate": "2026-10-15T20:00:00",
                "token": "synthetic-secret",
            }
        ]
        alarm = clean_world(world)["alarms"][0]
        self.assertEqual(alarm["node"], "Thermostat 2")
        self.assertNotIn("token", alarm)

    def test_discovery_action_topic_and_reset_template(self):
        _, config = discovery(
            Settings("broker"), "controller", clean_world(WORLD)["rooms"][0]
        )
        self.assertEqual(config["action_topic"], config["temperature_state_topic"])
        self.assertIn("'None'", config["action_template"])


class StatusPublishingTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.MQTTBridgeTests.asyncSetUp
    asyncTearDown = fixtures.MQTTBridgeTests.asyncTearDown

    # Reuses the existing isolated synthetic bridge fixture.
    async def test_controller_discovery_and_alarm_clear(self):
        b = self.bridge
        world = copy.deepcopy(WORLD)
        world["allUnresolvedAlarms"] = [
            {"id": 8, "node": "Thermostat 2", "room": "Køkken", "level": "Warning"}
        ]
        b.publish_snapshot(clean_world(world))

        def published(suffix):
            return [
                c for c in self.sdk.publish.call_args_list if c.args[0].endswith(suffix)
            ]

        configs = [json.loads(c.args[1]) for c in published("/config")]
        self.assertEqual(
            {c["name"] for c in configs},
            {"Køkken", "Systemstatus", "Alarm", "Antal alarmer"},
        )
        alarm = next(c for c in configs if c["name"] == "Alarm")
        self.assertEqual(alarm["device_class"], "problem")
        self.assertNotIn("command_topic", alarm)
        self.assertEqual(
            json.loads(published("/system/state")[-1].args[1]),
            {"current_state": "Asleep"},
        )
        self.assertEqual(
            json.loads(published("/alarms/state")[-1].args[1]), {"count": 1}
        )
        self.assertEqual(
            len(json.loads(published("/alarms/attributes")[-1].args[1])["alarms"]), 1
        )
        b.publish_snapshot(clean_world(WORLD))
        self.assertEqual(
            json.loads(published("/alarms/state")[-1].args[1]), {"count": 0}
        )
        self.assertEqual(
            json.loads(published("/alarms/attributes")[-1].args[1]), {"alarms": []}
        )

    async def test_failed_poll_marks_offline_without_clearing_alarms(self):
        b = self.bridge
        with patch(
            "mqtt_bridge.cloud_operation",
            new_callable=AsyncMock,
            side_effect=OSError("synthetic-secret"),
        ):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                task = asyncio.create_task(b.poll())
                await asyncio.sleep(0.02)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertNotIn("synthetic-secret", output.getvalue())
        self.assertFalse(b.cloud_ready)
        calls = self.sdk.publish.call_args_list
        self.assertTrue(
            any(c.args[:2] == (b.base + "/availability", "offline") for c in calls)
        )
        self.assertFalse(any(c.args[0].endswith("/alarms/state") for c in calls))

    async def test_snapshot_is_one_get_no_acknowledgement(self):
        transport = AsyncMock()
        transport.exchange.return_value = {
            "id": 1000000,
            "data": {"data": {"status": 200, "content": WORLD}},
        }
        result = await read_snapshot(transport)
        self.assertEqual(result["current_state"], "Asleep")
        body = transport.exchange.call_args.args[0]["data"]["data"]
        self.assertEqual((body["method"], body["uri"]), ("GET", "/api/world"))
