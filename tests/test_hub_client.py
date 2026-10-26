import unittest
from danfoss_readonly import ProtocolError
from hub_client import HubClient, HubPrecondition, HubUnverified
from hub_fixtures import BLOCKED, FakeCC
from hub_transport import HubRejected


def methods(cc):
    return [r[0] for r in cc.requests]


class RoomWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_setpoint_and_noop(self):
        cc = FakeCC()
        client = HubClient(cc, allow_writes=True)
        snapshot = await client.set_room(5, "setpoint", 20.5)
        self.assertEqual(snapshot["rooms"][1]["setpoint"], 20.5)
        self.assertEqual(methods(cc), ["GET", "PATCH", "GET"])
        self.assertEqual(cc.writes[0][2], {"id": 5, "setpoint": {"temperature": 20.5}})
        await client.set_room(5, "setpoint", 20.5)
        self.assertEqual(len(cc.writes), 1)

    async def test_athome_and_away_setpoints(self):
        cc = FakeCC()
        client = HubClient(cc, allow_writes=True)
        await client.set_room(1, "athome_setpoint", 21.5)
        snapshot = await client.set_room(1, "away_setpoint", 17.0)
        self.assertEqual((snapshot["rooms"][0]["athome_setpoint"], snapshot["rooms"][0]["away_setpoint"]), (21.5, 17.0))
        self.assertEqual(cc.writes[1][2], {"id": 1, "regulation": {"awaySetpoint": 17.0}})

    async def test_pause_blocks_setpoint_without_sending(self):
        cc = FakeCC()
        cc.mode("Pause")["state"] = "Active"
        with self.assertRaises(HubPrecondition):
            await HubClient(cc, allow_writes=True).set_room(5, "setpoint", 21)
        self.assertEqual(cc.writes, [])

    async def test_bounds_unknown_room_and_disabled_writes(self):
        for args in ((5, "setpoint", 29), (9, "setpoint", 20), (5, "setpoint", 5.5)):
            cc = FakeCC()
            with self.assertRaises(HubRejected):
                await HubClient(cc, allow_writes=True).set_room(*args)
            self.assertEqual(cc.writes, [])
        cc = FakeCC()
        with self.assertRaises(ProtocolError):
            await HubClient(cc).set_room(5, "setpoint", 21)
        self.assertEqual(cc.writes, [])
        with self.assertRaises(ProtocolError):
            await HubClient(cc, allow_writes=True).set_room(5, "regulation", 21)

    async def test_uncertain_and_unconfirmed_writes_never_retried(self):
        for cc in (FakeCC(fail_write=True), FakeCC(ignore_writes=True)):
            with self.assertRaises(HubUnverified) as error:
                await HubClient(cc, allow_writes=True).set_room(5, "setpoint", 21)
            self.assertNotIn("synthetic-secret", str(error.exception))
            self.assertEqual(len(cc.writes), 1)

    async def test_cc_rejection_carries_messages(self):
        cc = FakeCC()
        client = HubClient(cc, allow_writes=True)

        async def stale_read(method, uri, content=None):
            # The CC entered Pause between our read and the write.
            if method == "PATCH":
                raise HubRejected(400, [BLOCKED])
            return await FakeCC.request(cc, method, uri, content)

        cc.request = stale_read
        with self.assertRaises(HubRejected) as error:
            await client.set_room(5, "setpoint", 21)
        self.assertEqual(error.exception.messages, [BLOCKED])


class ModeAndSettingTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_round_trip(self):
        cc = FakeCC()
        client = HubClient(cc, allow_writes=True)
        snapshot = await client.set_mode("pause", True)
        self.assertEqual(snapshot["current_state"], "Pause")
        snapshot = await client.set_mode("pause", False)
        self.assertEqual(snapshot["current_state"], "Manual")
        self.assertEqual(
            [w[2] for w in cc.writes],
            [{"type": "Pause", "state": "Active"}, {"type": "Pause", "state": "Inactive"}],
        )
        await client.set_mode("pause", False)
        self.assertEqual(len(cc.writes), 2)

    async def test_vacation_needs_window(self):
        cc = FakeCC()
        client = HubClient(cc, allow_writes=True)
        with self.assertRaises(HubPrecondition):
            await client.set_mode("vacation", True, None, "2027-01-02T18:00:00")
        with self.assertRaises(ProtocolError):
            await client.set_mode("pause", True, "2026-12-20T08:00:00", "2027-01-02T18:00:00")
        snapshot = await client.set_mode("vacation", True, "2026-12-20T08:00:00", "2027-01-02T18:00:00")
        self.assertEqual(snapshot["modes"]["vacation"]["end"], "2027-01-02T18:00:00")
        self.assertEqual(len(cc.writes), 1)

    async def test_temperature_settings_bounds(self):
        cc = FakeCC()
        client = HubClient(cc, allow_writes=True)
        snapshot = await client.set_temperature_setting("pause", 7.5)
        self.assertEqual(snapshot["settings"]["pause"]["setpoint"], 7.5)
        self.assertEqual(cc.writes[0][2], {"pauseTemperature": {"setpoint": 7.5}})
        with self.assertRaises(HubPrecondition):
            await client.set_temperature_setting("pause", 10.5)
        self.assertEqual(len(cc.writes), 1)

    async def test_onoff_device(self):
        cc = FakeCC()
        cc.world["onOffDevices"] = [{"id": 3, "name": "Pump", "state": "Off"}]
        client = HubClient(cc, allow_writes=True)
        snapshot = await client.set_onoff(3, True)
        self.assertTrue(snapshot["onoff"][0]["on"])
        with self.assertRaises(HubPrecondition):
            await client.set_onoff(4, True)


if __name__ == "__main__":
    unittest.main()
