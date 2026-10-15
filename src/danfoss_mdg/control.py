"""Authenticated MDG control channel. No room writes are implemented here."""

import asyncio
from pathlib import Path
import ssl
from .framing import read_frame, send_frame
from .protobuf import SCHEMAS, packet, decode

HOSTS = ("77.66.11.90", "77.66.11.92", "5.179.92.180", "5.179.92.182")
MODULE = "mdg_control_pb"
CODES = SCHEMAS[MODULE]["codes"]
MESSAGE_NAMES = {
    code: ".mdg.control." + name.removeprefix("MT_").lower()
    for name, code in CODES.items()
}


class ControlError(Exception):
    pass


def tls_context(key, certificate):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # MDG identifies the grid using its bundled trust anchors, not DNS names.
    # This matches the APK's CA-based control channel verification.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_verify_locations(
        cafile=str(Path(__file__).with_name("trusted-CAs.pem"))
    )
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(certificate), str(key))
    context.set_ciphers(
        "ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES128-SHA256:"
        "ECDHE-ECDSA-AES128-SHA:ECDHE-ECDSA-AES256-GCM-SHA384:"
        "ECDHE-ECDSA-AES256-SHA384:ECDHE-ECDSA-AES256-SHA"
    )
    return context


class ControlChannel:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self._id = 0
        self._lock = asyncio.Lock()

    @classmethod
    async def connect(
        cls, key, certificate, host=HOSTS[0], timeout=10, compatibility=False
    ):
        if host not in HOSTS:
            raise ControlError("Unrecognized control host")
        try:
            if compatibility:
                from .tls_stream import open_stream, TLSError

                try:
                    stream = await open_stream(host, key, certificate, timeout)
                except TLSError as error:
                    raise ControlError(str(error)) from None
                reader = writer = stream
            else:
                context = tls_context(key, certificate)
                async with asyncio.timeout(timeout):
                    reader, writer = await asyncio.open_connection(
                        host,
                        443,
                        ssl=context,
                        server_hostname=host,
                        ssl_handshake_timeout=timeout,
                    )
        except ssl.SSLCertVerificationError as error:
            # Numeric OpenSSL verification code contains no key, token or device data.
            raise ControlError(
                f"Grid certificate verification failed (OpenSSL code {error.verify_code})"
            ) from None
        except (ssl.SSLError, OSError, TimeoutError):
            raise ControlError("Grid TLS connection failed") from None
        channel = cls(reader, writer)
        channel.certificate_policy = getattr(writer, "policy", None)
        try:
            await channel.send(
                "handshake",
                {
                    "magic": 4036857000,
                    "version_major": 1,
                    "version_minor": 0,
                    "property": [
                        {"key": "app.package.name", "value": "com.danfoss.linkapp"},
                        {"key": "app.version.name", "value": "Android-1.3.10"},
                    ],
                },
            )
            await channel.verify_handshake(timeout)
            return channel
        except Exception:
            await channel.close()
            raise

    async def send(self, name, values):
        code = CODES["MT_" + name.upper()]
        await send_frame(
            self.writer, packet(MODULE, ".mdg.control." + name, values, code)
        )

    async def receive(self, timeout=15):
        data = await read_frame(self.reader, timeout)
        if data[0] not in MESSAGE_NAMES:
            raise ControlError("Unknown control packet")
        name = MESSAGE_NAMES[data[0]]
        return name.rsplit(".", 1)[1], decode(MODULE, name, data[1:])

    async def verify_handshake(self, timeout=15):
        name, fields = await self.receive(timeout)
        if name != "handshake":
            raise ControlError("Expected grid handshake, received type: " + name)
        if fields["magic"] != 4036857000:
            raise ControlError("Grid handshake magic mismatch")
        if (fields["version_major"], fields["version_minor"]) != (1, 0):
            raise ControlError("Unsupported grid control protocol version")

    async def ping(self, timeout=15):
        async with self._lock:
            self._id += 1
            await self.send("ping", {"req_id": self._id})
            async with asyncio.timeout(timeout):
                while True:
                    name, fields = await self.receive(timeout)
                    if name == "ping":
                        await self.send("ping_reply", {"req_id": fields["req_id"]})
                    elif name == "ping_reply" and fields["req_id"] == self._id:
                        return True
                    elif name == "bye":
                        raise ControlError("Grid closed control session")
                    elif name == "reconnect":
                        raise ControlError("Grid requested reconnection")
                    else:
                        # Name comes exclusively from the static APK schema.
                        # Never include packet fields, payloads or identifiers.
                        raise ControlError("Unexpected control reply type: " + name)

    async def place_pairing_call(self, pair_id, timeout=30):
        if (
            not isinstance(pair_id, str)
            or not 1 <= len(pair_id) <= 64
            or not pair_id.isascii()
        ):
            raise ControlError("Invalid pairing identifier")
        async with self._lock:
            self._id += 1
            await self.send(
                "place_pairing_call", {"req_id": self._id, "pair_id": pair_id}
            )
            async with asyncio.timeout(timeout):
                while True:
                    name, fields = await self.receive(timeout)
                    if name == "ping":
                        await self.send("ping_reply", {"req_id": fields["req_id"]})
                    elif (
                        name == "place_pairing_call_reply"
                        and fields["req_id"] == self._id
                    ):
                        if fields["result"] != 0:
                            label = {1: "peer not available", 2: "server error"}.get(
                                fields["result"], "unknown result"
                            )
                            # Enum number and static label only; never result_text
                            # because the server can echo input identifiers there.
                            raise ControlError(
                                f"Grid pairing refused: {label} (result {fields['result']})"
                            )
                        if "routing" not in fields:
                            raise ControlError("Grid pairing reply omitted routing")
                        return fields["routing"]
                    else:
                        raise ControlError(
                            "Unexpected pairing control reply type: " + name
                        )

    async def place_read_call(self, peer_name, timeout=30):
        if (
            not isinstance(peer_name, str)
            or not 1 <= len(peer_name) <= 128
            or not peer_name.isascii()
        ):
            raise ControlError("Invalid saved peer identifier")
        async with self._lock:
            self._id += 1
            await self.send(
                "place_call",
                {
                    "req_id": self._id,
                    "peer_name": peer_name,
                    "protocol": "cc_json",
                    "ticket": "",
                },
            )
            async with asyncio.timeout(timeout):
                for _ in range(32):
                    name, fields = await self.receive(timeout)
                    if name == "ping":
                        await self.send("ping_reply", {"req_id": fields["req_id"]})
                    elif name == "place_call_reply" and fields["req_id"] == self._id:
                        if fields["result"] != 0:
                            raise ControlError("Grid refused paired read connection")
                        route = fields.get("routing")
                        if not route or route["peer_name"] != peer_name:
                            raise ControlError("Grid referral differs from saved peer")
                        return route
                    else:
                        raise ControlError("Unexpected read call control reply")
            raise ControlError("Read call reply limit reached")

    async def close(self):
        self.writer.close()
        try:
            async with asyncio.timeout(3):
                await self.writer.wait_closed()
        except (TimeoutError, OSError):
            pass
