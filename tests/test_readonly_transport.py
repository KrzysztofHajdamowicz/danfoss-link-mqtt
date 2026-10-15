import unittest, asyncio, tempfile, json, base64, os
from pathlib import Path
from unittest.mock import AsyncMock, patch
from danfoss_mdg.readonly_transport import (
    PairedReadTransport,
    load_trust,
    certificate_peer_id,
)
from danfoss_mdg.peer_certificate import candidate_public_key
from danfoss_mdg.pairing import PairingError
from danfoss_readonly import read_request, ProtocolError


class PinTests(unittest.TestCase):
    def test_mismatched_saved_certificate_rejected_before_use(self):
        with self.assertRaises(PairingError):
            candidate_public_key(b"unknown", trusted_der=b"paired")

    def test_certificate_identifier_format_and_missing_extension(self):
        from subprocess import CompletedProcess

        value = b"AA:BB:CC:DD:EE:FF:00:11:22:33:44:55:66:77:88:99:AA:BB:CC:DD"
        with patch(
            "danfoss_mdg.readonly_transport.subprocess.run",
            return_value=CompletedProcess(
                [], 0, b"X509v3 Subject Key Identifier:\n " + value, b""
            ),
        ):
            self.assertEqual(
                certificate_peer_id(b"synthetic-DER"), value.decode().lower()
            )
        with patch(
            "danfoss_mdg.readonly_transport.subprocess.run",
            return_value=CompletedProcess([], 0, b"no extension", b""),
        ):
            with self.assertRaises(ProtocolError):
                certificate_peer_id(b"synthetic-DER")

    def test_missing_or_world_readable_state_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            with self.assertRaises(FileNotFoundError):
                load_trust(root)
            for name in ("app_key.pem", "app_cert.pem", "paired_cc.json"):
                p = root / name
                p.write_text("{}")
                p.chmod(0o600)
            (root / "app_key.pem").chmod(0o644)
            with self.assertRaises(ProtocolError):
                load_trust(root)


class ReadTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_forged_write_or_extra_headers_never_sent(self):
        peer = AsyncMock()
        transport = PairedReadTransport(None, peer)
        for mutate in ("method", "headers", "extra"):
            req = read_request(1, "/api/rooms")
            if mutate == "method":
                req["data"]["data"]["method"] = "PATCH"
            elif mutate == "headers":
                req["data"]["data"]["headers"].append("secret-header")
            else:
                req["extra"] = "injected"
            with self.assertRaises(ProtocolError):
                await transport.exchange(req)
        peer.write.assert_not_called()

    async def test_valid_get_and_json_response(self):
        peer = AsyncMock()
        transport = PairedReadTransport(None, peer)
        req = read_request(1, "/api/rooms")
        response = {"id": 1, "data": {"data": {"status": 200, "content": []}}}
        with (
            patch(
                "danfoss_mdg.readonly_transport.send_frame", new_callable=AsyncMock
            ) as send,
            patch(
                "danfoss_mdg.readonly_transport.read_frame",
                new_callable=AsyncMock,
                return_value=json.dumps(response).encode(),
            ),
        ):
            self.assertEqual(await transport.exchange(req), response)
            self.assertEqual(json.loads(send.call_args.args[1]), req)
