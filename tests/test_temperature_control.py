import unittest, math
from unittest.mock import AsyncMock, patch
from danfoss_client import DanfossClient, temperature_request
from danfoss_readonly import ProtocolError
from danfoss_mdg.control_transport import PairedControlTransport


class Fake:
    def __init__(self, *, mismatch=False, fail_patch=False):
        self.requests = []
        self.target = 17.0
        self.mismatch = mismatch
        self.fail_patch = fail_patch

    async def exchange(self, req):
        self.requests.append(req)
        body = req["data"]["data"]
        if body["method"] == "GET":
            content = [
                {
                    "id": 77,
                    "name": "Synthetic room",
                    "minSetpoint": 6,
                    "maxSetpoint": 25,
                    "setpoint": {"temperature": self.target},
                }
            ]
        else:
            if self.fail_patch:
                raise OSError("synthetic-secret-that-must-not-escape")
            if not self.mismatch:
                self.target = body["content"]["setpoint"]["temperature"]
            content = {"id": 77}
        return {
            "id": req["id"],
            "data": {
                "type": "JsonWebResponse",
                "data": {"status": 200, "content": content},
            },
        }


class TemperatureTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_patch_and_readback(self):
        transport = Fake()
        client = DanfossClient(transport, allow_writes=True)
        result = await client.set_temperature(77, 17.5)
        self.assertEqual(result["setpoint"]["temperature"], 17.5)
        self.assertEqual(
            [r["data"]["data"]["method"] for r in transport.requests],
            ["GET", "PATCH", "GET"],
        )
        self.assertEqual(transport.requests[1], temperature_request(2, 77, 17.5))

    async def test_disabled_and_invalid_inputs_never_sent(self):
        transport = Fake()
        client = DanfossClient(transport)
        with self.assertRaises(ProtocolError):
            await client.set_temperature(77, 17.5)
        self.assertFalse(transport.requests)
        for value in (True, float("nan"), float("inf"), 5, 36, "17.5"):
            with self.assertRaises(ProtocolError):
                temperature_request(1, 77, value)

    async def test_unknown_room_and_bounds_never_patch(self):
        for room, temp in ((78, 17.5), (77, 26)):
            transport = Fake()
            client = DanfossClient(transport, allow_writes=True)
            with self.assertRaises(ProtocolError):
                await client.set_temperature(room, temp)
            self.assertTrue(
                all(r["data"]["data"]["method"] == "GET" for r in transport.requests)
            )

    async def test_uncertain_write_not_retried_and_secret_suppressed(self):
        transport = Fake(fail_patch=True)
        client = DanfossClient(transport, allow_writes=True)
        with self.assertRaises(ProtocolError) as error:
            await client.set_temperature(77, 17.5)
        self.assertNotIn("synthetic-secret", str(error.exception))
        self.assertEqual(
            sum(r["data"]["data"]["method"] == "PATCH" for r in transport.requests), 1
        )

    async def test_mismatched_readback_rejected_without_retry(self):
        transport = Fake(mismatch=True)
        client = DanfossClient(transport, allow_writes=True)
        with self.assertRaises(ProtocolError):
            await client.set_temperature(77, 17.5)
        self.assertEqual(
            sum(r["data"]["data"]["method"] == "PATCH" for r in transport.requests), 1
        )

    async def test_forged_extra_write_fields_rejected(self):
        transport = PairedControlTransport(None, None)
        transport.allow_writes = True
        for field in ("regulation", "schedule", "zone"):
            req = temperature_request(1, 77, 17.5)
            req["data"]["data"]["content"][field] = {}
            with self.assertRaises(ProtocolError):
                await transport.exchange(req)
        req = temperature_request(1, 77, 17.5)
        transport.allow_writes = False
        with self.assertRaises(ProtocolError):
            await transport.exchange(req)
