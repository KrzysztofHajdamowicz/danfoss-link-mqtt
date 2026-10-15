"""Allowlisted progress metadata only; never interpolate request payloads."""

from datetime import datetime, timezone
import os

EVENTS = {
    "identity_ready": "App identity ready",
    "control_ready": "Authenticated grid connected",
    "route_ready": "Grid assigned pairing route",
    "router_hello": "Router handshake sent",
    "router_hold": "Router requested wait",
    "router_ready": "Router selected TLS 1.2",
    "peer_tls_start": "Starting peer TLS handshake",
    "peer_tls_ready": "Peer TLS handshake complete",
    "peer_tls_identity": "Peer TLS app identity presentation",
    "peer_certificate": "Inspecting peer certificate chain",
    "peer_issuer_missing": "Issuer unavailable; pairing-only PACE bootstrap used",
    "public_certificates_saved": "Untrusted public certificate diagnostics saved",
    "pace_hello_sent": "PACE client hello sent",
    "pace_hello_received": "PACE server hello received",
    "pace_scalar": "PACE nonce decoded",
    "pace_xy_sent": "PACE XY sent",
    "pace_xy_received": "PACE XY received",
    "pace_pk_sent": "PACE PK sent",
    "pace_pk_received": "PACE PK received",
    "pace_proof_sent": "PACE proof sent",
    "pace_proof_received": "PACE peer proof received",
    "pace_proof_profile": "PACE proof diagnostic profile",
    "pace_verified": "PACE peer proof verified",
    "contact_sent": "Contact information sent after PACE",
    "contact_received": "Contact information received",
    "trust_saved": "Private CC trust record saved",
    "pair_failed": "Pairing attempt failed; no new CC trust saved",
}
INTEGER_FIELDS = {
    "chain_count",
    "cipher_bits",
    "router_count",
    "proof_bytes",
    "certificate_requests",
    "client_certificates",
}
CIPHERS = {
    "ECDHE-ECDSA-AES256-GCM-SHA384",
    "ECDHE-ECDSA-AES256-SHA384",
    "ECDHE-ECDSA-AES256-SHA",
}

PROOF_FORMATS = {"raw32", "lower_hex64", "upper_hex64", "other"}
PROOF_PROFILES = {"none"} | {
    k + "_" + p + "_" + f
    for k in ("sha_raw", "sha_hex", "hmac_empty_raw", "hmac_empty_hex")
    for p in ("own", "peer")
    for f in ("raw", "hex", "upper")
}


def progress(event, **metadata):
    if event not in EVENTS:
        raise ValueError("Unknown diagnostic event")
    for key, value in metadata.items():
        if key in INTEGER_FIELDS and type(value) is int and 0 <= value <= 65535:
            continue
        if key == "cipher" and value in CIPHERS:
            continue
        if key == "proof_format" and value in PROOF_FORMATS:
            continue
        if key == "profile" and value in PROOF_PROFILES:
            continue
        raise ValueError("Unapproved diagnostic metadata")
    timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    extra = "; ".join(key + "=" + str(value) for key, value in metadata.items())
    line = timestamp + " " + EVENTS[event] + (" | " + extra if extra else "")
    print(line, flush=True)
    # The idle runtime consumes this FIFO, so docker logs also shows only
    # these validated progress lines. Failure to tee never breaks pairing.
    try:
        fd = os.open(
            "/tmp/mdg/progress.pipe", os.O_WRONLY | os.O_NONBLOCK | os.O_NOFOLLOW
        )
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass
