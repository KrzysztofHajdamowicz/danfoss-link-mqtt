"""PACE pairing wire layer; no unauthenticated contact-info exchange."""

import hashlib
import hmac
from .framing import read_frame, send_frame
from .protobuf import SCHEMAS, packet, decode, WireError
from .diagnostics import progress

MODULE = "mdg_pairing_pb"
CODES = SCHEMAS[MODULE]["codes"]
NAMES = {
    1: ".pace_client_hello",
    2: ".pace_server_hello",
    3: ".pace_xy",
    4: ".pace_pk",
    5: ".pace_proof",
    6: ".contact_info",
}


class PairingError(Exception):
    """Safe fixed messages only: never interpolate a code or payload."""


def encode_pi(parts):
    """APK pace.lua L14: 16-bit BE lengths and final 0xff byte."""
    result = bytearray()
    for value in parts:
        if not isinstance(value, bytes) or len(value) > 65535:
            raise PairingError("Invalid PACE identity binding")
        result.extend(len(value).to_bytes(2, "big"))
        result.extend(value)
    result.append(255)
    return bytes(result)


def derive_pi_key(binding, salt):
    if not isinstance(binding, bytes) or not isinstance(salt, bytes):
        raise PairingError("Invalid PACE key inputs")
    return hashlib.pbkdf2_hmac("sha256", binding, salt, 1024, 32)[:16]


def shared_key(point_bytes):
    """APK callable crypto.digest receiver makes the result raw SHA256."""
    if (
        not isinstance(point_bytes, bytes)
        or len(point_bytes) != 81
        or point_bytes[0] != 4
    ):
        raise PairingError("Invalid PACE shared point encoding")
    return hashlib.sha256(point_bytes).digest()


def proof_token(key, peer_point):
    if not isinstance(key, bytes) or len(key) != 32:
        raise PairingError("Invalid PACE proof key")
    if not isinstance(peer_point, bytes) or len(peer_point) != 81 or peer_point[0] != 4:
        raise PairingError("Invalid PACE public point encoding")
    return hmac.new(key, peer_point, hashlib.sha256).hexdigest().encode("ascii")


def verify_proof(expected, received):
    return (
        isinstance(expected, bytes)
        and isinstance(received, bytes)
        and len(expected) == len(received) == 64
        and hmac.compare_digest(expected, received)
    )


class PairingChannel:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.authenticated = False

    async def send(self, name, fields):
        if name == "contact_info" and not self.authenticated:
            raise PairingError("PACE proof required before contact exchange")
        full_name = "." + name
        codes = {value: code for code, value in NAMES.items()}
        if full_name not in codes:
            raise PairingError("Unknown pairing message type")
        await send_frame(
            self.writer, packet(MODULE, full_name, fields, codes[full_name])
        )

    async def receive(self, expected, timeout=15):
        try:
            payload = await read_frame(self.reader, timeout)
        except PairingError as error:
            # Expected type is a client constant, never received secret data.
            raise PairingError(
                "While receiving " + expected + ": " + str(error)
            ) from None
        full_name = NAMES.get(payload[0])
        if full_name != "." + expected:
            raise PairingError("Unexpected pairing message type")
        try:
            return decode(MODULE, full_name, payload[1:])
        except WireError:
            raise PairingError("Invalid pairing message") from None

    async def hello(self):
        await self.send(
            "pace_client_hello",
            {
                "magic": 4036856999,
                "version_major": 1,
                "version_minor": 0,
            },
        )
        progress("pace_hello_sent")
        fields = await self.receive("pace_server_hello")
        progress("pace_hello_received")
        if (fields["version_major"], fields["version_minor"]) != (1, 0):
            raise PairingError("Unsupported PACE version")
        # AES block IV and encrypted 320-bit scalar with random padding.
        if len(fields["iv"]) != 16 or len(fields["z"]) != 64:
            raise PairingError("Invalid PACE scalar envelope")
        return fields

    async def authenticate(
        self, sent_token, expected_token, diagnostic_candidates=None
    ):
        await self.send("pace_proof", {"token": sent_token})
        progress("pace_proof_sent")
        fields = await self.receive("pace_proof")
        received = fields["token"]
        fmt = "other"
        if len(received) == 32:
            fmt = "raw32"
        elif len(received) == 64 and all(c in b"0123456789abcdef" for c in received):
            fmt = "lower_hex64"
        elif len(received) == 64 and all(c in b"0123456789ABCDEF" for c in received):
            fmt = "upper_hex64"
        progress("pace_proof_received", proof_bytes=len(received), proof_format=fmt)
        if diagnostic_candidates is not None:
            profile = "none"
            for label, candidate in diagnostic_candidates.items():
                if hmac.compare_digest(candidate, received):
                    profile = label
                    break
            progress("pace_proof_profile", profile=profile)
        if not verify_proof(expected_token, fields["token"]):
            raise PairingError("PACE proof verification failed")
        self.authenticated = True
        progress("pace_verified")
