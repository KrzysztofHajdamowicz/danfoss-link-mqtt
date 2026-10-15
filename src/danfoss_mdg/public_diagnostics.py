"""Public certificates only: this file is never a trusted pairing record."""

import base64
import json
import os
from pathlib import Path
import tempfile
from .pairing import PairingError


def save_public_candidates(state, own_der, peer_der, own_public, peer_public):
    for certificate in (own_der, peer_der):
        if not isinstance(certificate, bytes) or not 1 <= len(certificate) <= 65535:
            raise PairingError("Invalid public diagnostic certificate")
    for public in (own_public, peer_public):
        if (
            not isinstance(public, bytes)
            or len(public) > 8192
            or not public.startswith(b"-----BEGIN PUBLIC KEY-----\n")
            or not public.endswith(b"-----END PUBLIC KEY-----\n")
        ):
            raise PairingError("Invalid public diagnostic key")
    record = {
        "format": 1,
        "status": "untrusted-diagnostic-only",
        "own_certificate_der": base64.b64encode(own_der).decode("ascii"),
        "peer_certificate_der": base64.b64encode(peer_der).decode("ascii"),
        "own_exported_public_pem": own_public.decode("ascii"),
        "peer_exported_public_pem": peer_public.decode("ascii"),
    }
    destination = Path(state) / "untrusted-public-certificates.json"
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=state, delete=False) as output:
            temporary = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            json.dump(record, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
