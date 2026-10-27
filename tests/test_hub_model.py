import copy, json, re, unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from danfoss_readonly import ProtocolError
from hub_fixtures import WORLD
from hub_model import (
    VERSION,
    aware_to_cc,
    hub_discovery,
    hub_state,
    parse_world,
    room_discovery,
    room_state,
    setpoints_blocked,
)
from mqtt_bridge import Settings

WARSAW = ZoneInfo("Europe/Warsaw")
NONE = {"start": None, "end": None}


class ParseWorldTests(unittest.TestCase):
    def test_rooms_modes_settings_and_schedule(self):
        snapshot = parse_world(WORLD)
        room = snapshot["rooms"][0]
        self.assertEqual(
            {k: room[k] for k in ("id", "temperature", "setpoint", "mode", "action")},
            {"id": 1, "temperature": 22.6, "setpoint": 21.5, "mode": "heat", "action": "idle"},
        )
        self.assertEqual((room["athome_setpoint"], room["away_setpoint"]), (21.0, 18.0))
        self.assertEqual((room["min_temp"], room["max_temp"]), (6.0, 28.0))
        self.assertEqual(room["schedule"]["monday"][-1], {"state": "Away", "start": "22:00", "end": "24:00"})
        self.assertFalse(snapshot["modes"]["pause"]["active"])
        self.assertEqual(snapshot["settings"]["pause"], {"setpoint": 6.0, "min": 4.0, "max": 10.0})
        self.assertEqual(snapshot["system"]["version"], "4.2.1724")
        self.assertEqual(snapshot["onoff"], [])
        self.assertFalse(setpoints_blocked(snapshot))

    def test_untrusted_optional_fields_degrade_to_unknown(self):
        world = copy.deepcopy(WORLD)
        world["modes"] = [{"type": "Pause", "state": "Active", "startTime": "<script>"}, None, "x"]
        world["temperatureSettings"] = {"pauseTemperature": {"setpoint": "6"}}
        world["systemInfo"] = {"name": "bad\x00name", "version": 4}
        world["allRooms"][0]["regulation"] = {"type": "Zoned", "atHomeSetpoint": 99}
        world["allRooms"][0]["schedule"] = {
            "monday": [
                {"state": "AtHome", "startTimeFromMidnight": "25:00:00", "endTimeFromMidnight": "26:00:00"},
                {"state": "Away\n", "startTimeFromMidnight": "01:00:00", "endTimeFromMidnight": "02:00:00"},
            ]
        }
        world["onOffDevices"] = [{"id": 2, "name": "Pump", "state": "On"}, {"id": 2}, {"id": "3"}]
        snapshot = parse_world(world)
        self.assertEqual(snapshot["modes"]["pause"], {"active": True, "state": "Active", "start": None, "end": None})
        self.assertIsNone(snapshot["modes"]["vacation"])
        self.assertEqual(snapshot["settings"], {"pause": None, "vacation": None})
        self.assertEqual(snapshot["system"], {"name": None, "serial": None, "version": None})
        room = snapshot["rooms"][0]
        self.assertEqual((room["regulation"], room["mode"], room["athome_setpoint"]), (None, "heat", None))
        self.assertEqual(room["schedule"]["monday"], [])
        self.assertEqual(snapshot["onoff"], [{"id": 2, "name": "Pump", "on": True}])
        self.assertTrue(setpoints_blocked(snapshot))

    def test_core_fields_and_duplicate_rooms_rejected(self):
        world = copy.deepcopy(WORLD)
        world["allRooms"].append(copy.deepcopy(world["allRooms"][0]))
        with self.assertRaises(ProtocolError):
            parse_world(world)
        world = copy.deepcopy(WORLD)
        del world["systemStatus"]
        with self.assertRaises(ProtocolError):
            parse_world(world)

    def test_schedule_mode_maps_to_auto(self):
        world = copy.deepcopy(WORLD)
        world["allRooms"][1]["regulation"]["type"] = "Schedule"
        self.assertEqual(parse_world(world)["rooms"][1]["mode"], "auto")


class StateTests(unittest.TestCase):
    def test_hub_state_and_holiday_window(self):
        world = copy.deepcopy(WORLD)
        state = hub_state(parse_world(world), WARSAW, NONE)
        self.assertEqual(
            state,
            {
                "pause": "OFF",
                "vacation": "OFF",
                "athome": "OFF",
                "vacation_start": None,
                "vacation_end": None,
                "pause_temperature": 6.0,
                "vacation_temperature": 6.0,
            },
        )
        pending = {"start": None, "end": datetime(2027, 1, 2, 17, tzinfo=timezone.utc)}
        self.assertEqual(hub_state(parse_world(world), WARSAW, pending)["vacation_end"], "2027-01-02T17:00:00+00:00")
        vacation = next(m for m in world["modes"] if m["type"] == "Vacation")
        vacation.update(state="Active", startTime="2026-12-20T08:00:00", endTime="2027-01-02T18:00:00")
        state = hub_state(parse_world(world), WARSAW, pending)
        self.assertEqual(state["vacation"], "ON")
        self.assertEqual(state["vacation_end"], "2027-01-02T18:00:00+01:00")

    def test_cc_local_time_conversion(self):
        moment = datetime(2026, 7, 1, 6, 30, tzinfo=timezone.utc)
        self.assertEqual(aware_to_cc(moment, WARSAW), "2026-07-01T08:30:00")

    def test_room_state_keeps_flat_bridge_keys(self):
        state = room_state(parse_world(WORLD)["rooms"][0])
        for key in ("temperature", "setpoint", "mode", "action"):
            self.assertIn(key, state)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = parse_world(WORLD)

    def check_device_payload(self, payload):
        self.assertIn("identifiers", payload["device"])
        self.assertEqual(payload["origin"]["name"], "danfoss-link-mqtt")
        self.assertEqual(payload["availability"], [{"topic": "danfoss_link/cid/availability"}])
        uids = [c["unique_id"] for c in payload["components"].values()]
        self.assertEqual(len(uids), len(set(uids)))
        for component in payload["components"].values():
            self.assertIn("platform", component)
            if "command_topic" in component:
                self.assertFalse(component["retain"])
        json.dumps(payload, allow_nan=False)

    def test_hub_device_entities_and_flat_compatible_ids(self):
        topic, payload = hub_discovery(Settings("broker"), "cid", self.snapshot)
        self.assertEqual(topic, "homeassistant/device/danfoss_link_cid/config")
        self.check_device_payload(payload)
        components = payload["components"]
        self.assertEqual(
            {k: c["platform"] for k, c in components.items()},
            {
                "status": "sensor",
                "problem": "binary_sensor",
                "alarms": "sensor",
                "pause": "switch",
                "vacation": "switch",
                "athome": "switch",
                "vacation_start": "datetime",
                "vacation_end": "datetime",
                "pause_temperature": "number",
                "vacation_temperature": "number",
                "last_command": "sensor",
            },
        )
        for key, flat in (("status", "system_state"), ("problem", "alarm"), ("alarms", "alarm_count")):
            self.assertEqual(components[key]["unique_id"], "danfoss_link_cid_" + flat)
        self.assertEqual(components["pause"]["command_topic"], "danfoss_link/cid/hub/pause/set")
        self.assertEqual((components["pause_temperature"]["min"], components["pause_temperature"]["max"]), (4.0, 10.0))
        self.assertEqual(payload["device"]["sw_version"], "4.2.1724")

    def test_readonly_hub_has_no_command_topics(self):
        _, payload = hub_discovery(Settings("broker", allow_writes=False), "cid", self.snapshot)
        self.check_device_payload(payload)
        self.assertNotIn("command_topic", json.dumps(payload))
        self.assertNotIn("last_command", payload["components"])
        self.assertEqual(payload["components"]["pause"]["platform"], "binary_sensor")
        self.assertEqual(payload["components"]["vacation_end"]["device_class"], "timestamp")

    def test_room_device_is_child_of_hub(self):
        room = self.snapshot["rooms"][1]
        topic, payload = room_discovery(Settings("broker"), "cid", room)
        self.assertEqual(topic, "homeassistant/device/danfoss_link_cid_room_5/config")
        self.check_device_payload(payload)
        device = payload["device"]
        self.assertEqual(device["via_device"], "danfoss_link_cid")
        self.assertEqual(device["suggested_area"], "Køkken")
        climate = payload["components"]["climate"]
        self.assertIsNone(climate["name"])
        self.assertEqual(climate["unique_id"], "danfoss_link_cid_room_5")
        self.assertNotIn("mode_command_topic", climate)
        self.assertEqual(climate["temperature_command_topic"], "danfoss_link/cid/room/5/temperature/set")
        _, payload = room_discovery(Settings("broker", allow_writes=False), "cid", room, suggest_area=False)
        self.assertNotIn("suggested_area", payload["device"])
        self.assertNotIn("command_topic", json.dumps(payload))

    def test_addon_and_project_versions_match_origin(self):
        root = Path(__file__).parents[1]
        config = (root / "danfoss_link_cc_mqtt" / "config.yaml").read_text()
        project = (root / "pyproject.toml").read_text()
        self.assertEqual(re.search(r'^version: "([^"]+)"', config, re.M).group(1), VERSION)
        self.assertEqual(re.search(r'^version = "([^"]+)"', project, re.M).group(1), VERSION)


if __name__ == "__main__":
    unittest.main()
