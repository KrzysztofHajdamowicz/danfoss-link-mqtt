import json, unittest
from unittest.mock import AsyncMock, patch
from danfoss_readonly import ProtocolError
from hub_transport import HubRejected, HubTransport, hub_content, hub_request

VALID = [
    ("GET", "/api/world", None),
    ("GET", "/api/rooms/5", None),
    ("GET", "/api/settings/temperature", None),
    ("PATCH", "/api/rooms/5", {"id": 5, "setpoint": {"temperature": 21.5}}),
    ("PATCH", "/api/rooms/5", {"id": 5, "regulation": {"atHomeSetpoint": 21.0}}),
    ("PATCH", "/api/rooms/5", {"id": 5, "regulation": {"awaySetpoint": 17.0}}),
    ("PUT", "/api/modes/pause", {"type": "Pause", "state": "Active"}),
    ("PUT", "/api/modes/athome", {"type": "AtHome", "state": "Inactive"}),
    (
        "PUT",
        "/api/modes/vacation",
        {
            "type": "Vacation",
            "state": "Active",
            "startTime": "2026-12-20T08:00:00",
            "endTime": "2027-01-02T18:00:00",
        },
    ),
    ("PATCH", "/api/settings/temperature", {"pauseTemperature": {"setpoint": 7.0}}),
    ("PATCH", "/api/settings/temperature", {"vacationTemperature": {"setpoint": 12.0}}),
    ("PATCH", "/api/onoffdevices/3", {"id": 3, "state": "On"}),
]

INVALID = [
    ("GET", "/api/world", {"x": 1}),
    ("GET", "/api/zones/living", None),
    ("GET", "/api/system/software", None),
    ("DELETE", "/api/rooms/5", None),
    ("PATCH", "/api/rooms/5", {"id": 6, "setpoint": {"temperature": 21.5}}),
    ("PATCH", "/api/rooms/5", {"id": 5, "setpoint": {"temperature": 36}}),
    ("PATCH", "/api/rooms/5", {"id": 5, "setpoint": {"temperature": True}}),
    ("PATCH", "/api/rooms/5", {"id": 5, "setpoint": {"temperature": float("nan")}}),
    ("PATCH", "/api/rooms/5", {"id": 5, "setpoint": {"temperature": 21, "type": "x"}}),
    ("PATCH", "/api/rooms/5", {"id": 5, "regulation": {"type": "Schedule"}}),
    ("PATCH", "/api/rooms/5", {"id": 5, "regulation": {"atHomeSetpoint": 21, "awaySetpoint": 17}}),
    ("PATCH", "/api/rooms/5", {"id": 5, "setpoint": {"temperature": 21}, "schedule": {}}),
    ("PATCH", "/api/rooms/5", {"id": 5, "name": "renamed"}),
    ("PUT", "/api/modes/pause", {"type": "Vacation", "state": "Active"}),
    ("PUT", "/api/modes/pause", {"type": "Pause", "state": "On"}),
    ("PUT", "/api/modes/boost", {"type": "Boost", "state": "Active"}),
    (
        "PUT",
        "/api/modes/pause",
        {"type": "Pause", "state": "Active", "startTime": "2026-12-20T08:00:00", "endTime": "2027-01-02T18:00:00"},
    ),
    (
        "PUT",
        "/api/modes/vacation",
        {"type": "Vacation", "state": "Active", "startTime": "2027-01-02T18:00:00", "endTime": "2026-12-20T08:00:00"},
    ),
    (
        "PUT",
        "/api/modes/vacation",
        {"type": "Vacation", "state": "Inactive", "startTime": "2026-12-20T08:00:00", "endTime": "2027-01-02T18:00:00"},
    ),
    ("PUT", "/api/modes/vacation", {"type": "Vacation", "state": "Active", "startTime": "soon", "endTime": "later"}),
    ("PATCH", "/api/settings/temperature", {"pauseTemperature": {"setpoint": 11}}),
    ("PATCH", "/api/settings/temperature", {"pauseTemperature": {"setpoint": 6}, "vacationTemperature": {"setpoint": 6}}),
    ("PATCH", "/api/settings/temperature", {"temperatureScale": "Fahrenheit"}),
    ("PATCH", "/api/onoffdevices/3", {"id": 3, "state": "Toggle"}),
    ("PATCH", "/api/system/info", {"name": "x"}),
]


class AllowlistTests(unittest.TestCase):
    def test_verified_shapes_build_exact_envelopes(self):
        for method, uri, content in VALID:
            request = hub_request(7, method, uri, content)
            data = request["data"]["data"]
            self.assertEqual(request["id"], 7)
            self.assertEqual((data["method"], data["uri"]), (method, uri))
            self.assertEqual(data.get("content"), content)
            self.assertEqual(len(data["headers"]), 1 if content is None else 2)

    def test_everything_else_rejected_before_sending(self):
        for method, uri, content in INVALID:
            with self.subTest(method=method, uri=uri, content=content):
                with self.assertRaises(ProtocolError):
                    hub_request(1, method, uri, content)

    def test_rejection_messages_are_sanitized_and_bounded(self):
        response = {
            "id": 3,
            "data": {
                "data": {
                    "status": 400,
                    "content": {
                        "errorMessages": ["Blocked\x1b[31m", "x" * 500, None, 4, "a", "b", "c"]
                    },
                }
            },
        }
        with self.assertRaises(HubRejected) as error:
            hub_content(response, 3)
        self.assertEqual(error.exception.status, 400)
        self.assertEqual(error.exception.messages[0], "Blocked[31m")
        self.assertEqual(len(error.exception.messages[1]), 200)
        self.assertEqual(len(error.exception.messages), 2)
        for broken in ({}, {"id": 4, "data": {"data": {"status": 200}}}, {"id": 3, "data": None}):
            with self.assertRaises(ProtocolError):
                hub_content(broken, 3)


class HubTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_request_ids_increment_and_content_returned(self):
        transport = HubTransport(None, AsyncMock())
        sent = []

        async def send(peer, data):
            sent.append(json.loads(data))

        async def read(peer, timeout):
            request = sent[-1]
            return json.dumps(
                {"id": request["id"], "data": {"data": {"status": 200, "content": {"ok": request["id"]}}}}
            ).encode()

        with (
            patch("danfoss_mdg.readonly_transport.send_frame", side_effect=send),
            patch("danfoss_mdg.readonly_transport.read_frame", side_effect=read),
        ):
            self.assertEqual(await transport.request("GET", "/api/world"), {"ok": 1})
            self.assertEqual(
                await transport.request("PUT", "/api/modes/pause", {"type": "Pause", "state": "Inactive"}),
                {"ok": 2},
            )
        self.assertEqual(sent[1], hub_request(2, "PUT", "/api/modes/pause", {"type": "Pause", "state": "Inactive"}))

    async def test_forged_request_never_reaches_peer(self):
        transport = HubTransport(None, AsyncMock())
        with patch("danfoss_mdg.readonly_transport.send_frame", new_callable=AsyncMock) as send:
            with self.assertRaises(ProtocolError):
                await transport.request("PATCH", "/api/rooms/5", {"id": 5, "zone": "Sleeping"})
            send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
