"""Client side of APK PACE; call only on the intended peer TLS channel."""

import asyncio
import hashlib
import hmac
from .pairing import (
    PairingError,
    encode_pi,
    derive_pi_key,
    shared_key,
    proof_token,
)
from .pace_crypto import PaceCrypto
from .diagnostics import progress


async def authenticate_peer(channel, otp, peer_public_pem, own_public_pem):
    if not isinstance(otp, bytes) or len(otp) != 5:
        raise PairingError("Invalid PACE code length")
    if not all(
        isinstance(p, bytes)
        and p.startswith(b"-----BEGIN PUBLIC KEY-----")
        and len(p) <= 8192
        for p in (peer_public_pem, own_public_pem)
    ):
        raise PairingError("Invalid PACE public key binding")
    if channel.authenticated:
        raise PairingError("Pairing channel already authenticated")
    # Pairing procedure client binds OTP, server public PEM, client public PEM.
    async with asyncio.timeout(120):
        hello = await channel.hello()
        pi_key = derive_pi_key(
            encode_pi([otp, peer_public_pem, own_public_pem]), hello["salt"]
        )
        with PaceCrypto() as crypto:
            pi_scalar = crypto.decode_scalar(hello["z"], pi_key, hello["iv"])
            progress("pace_scalar")
            ephemeral = crypto.random_scalar()
            own_xy = crypto.multiply(ephemeral)
            await channel.send("pace_xy", {"xy": own_xy})
            progress("pace_xy_sent")
            peer_xy = (await channel.receive("pace_xy"))["xy"]
            progress("pace_xy_received")
            mapped = crypto.mapped_generator(
                pi_scalar, crypto.multiply(ephemeral, peer_xy)
            )
            ephemeral2 = crypto.random_scalar()
            own_pk = crypto.multiply(ephemeral2, mapped)
            await channel.send("pace_pk", {"pk": own_pk})
            progress("pace_pk_sent")
            peer_pk = (await channel.receive("pace_pk"))["pk"]
            progress("pace_pk_received")
            shared = crypto.multiply(ephemeral2, peer_pk)
            key = shared_key(shared)
            candidates = proof_profiles(shared, own_pk, peer_pk)
            await channel.authenticate(
                proof_token(key, peer_pk), proof_token(key, own_pk), candidates
            )
    return True


def proof_profiles(shared, own, peer):
    """In-memory diagnostics only. These never authorize pairing or get logged."""
    digest = shared_key(shared)
    empty_hmac = hmac.new(b"", shared, hashlib.sha256).digest()
    keys = {
        "sha_raw": digest,
        "sha_hex": digest.hex().encode("ascii"),
        "hmac_empty_raw": empty_hmac,
        "hmac_empty_hex": empty_hmac.hex().encode("ascii"),
    }
    candidates = {}
    for key_name, key in keys.items():
        for target, point in (("own", own), ("peer", peer)):
            if not isinstance(point, bytes) or len(point) != 81 or point[0] != 4:
                raise PairingError("Invalid diagnostic point encoding")
            raw = hmac.new(key, point, hashlib.sha256).digest()
            for fmt, value in (
                ("raw", raw),
                ("hex", raw.hex().encode("ascii")),
                ("upper", raw.hex().upper().encode("ascii")),
            ):
                candidates[key_name + "_" + target + "_" + fmt] = value
    return candidates
