"""Interactive pairing of owner's CC. Never place the code in argv/env/logs."""

import argparse
import asyncio
import base64
import getpass
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
from danfoss_mdg.control import ControlChannel, ControlError, HOSTS
from danfoss_mdg.identity import ensure_identity, IdentityError
from danfoss_mdg.pairing import PairingChannel, PairingError
from danfoss_mdg.pace_client import authenticate_peer
from danfoss_mdg.routing import connect_pairing_route
from danfoss_mdg.diagnostics import progress
from danfoss_mdg.public_diagnostics import save_public_candidates


def normalize_code(code):
    if not isinstance(code, str):
        raise PairingError("Invalid pairing code format")
    # APK S/j.java connectOtp strips these presentation separators.
    code = code.translate(str.maketrans("", "", "- .,")).strip()
    if not 6 <= len(code) <= 69 or not code.isascii() or not code.isdigit():
        raise PairingError("Invalid pairing code format")
    return code


def code_mode(code):
    return "local" if normalize_code(code).startswith("0000") else "cloud"


async def pair(state, code, name="Danfoss MQTT", *, identity_profile="legacy"):
    if code_mode(code) == "local":
        raise PairingError(
            "Local pairing code detected; cloud pairing was not attempted"
        )
    control = peer = None
    key, cert = ensure_identity(state, profile=identity_profile)
    progress("identity_ready")
    result = subprocess.run(
        ["openssl", "x509", "-in", str(cert), "-pubkey", "-noout"],
        capture_output=True,
        timeout=10,
    )
    if result.returncode:
        raise PairingError("Own public key export failed")
    own_public = result.stdout
    try:
        for host in HOSTS:
            try:
                control = await ControlChannel.connect(key, cert, host)
                break
            except ControlError:
                pass
        if control is None:
            raise PairingError("Authenticated grid connection unavailable")
        print(
            "Authenticated grid connected; requesting own CC pairing route", flush=True
        )
        progress("control_ready")
        route = await control.place_pairing_call(code[:-5])
        progress("route_ready", router_count=len(route["router"]))
        peer = await connect_pairing_route(route, key, cert)
        print(
            "Peer TLS established (256-bit cipher); starting panel-code PACE",
            flush=True,
        )
        result = subprocess.run(
            ["openssl", "x509", "-in", str(cert), "-outform", "DER"],
            capture_output=True,
            timeout=10,
        )
        if result.returncode:
            raise PairingError("Own public certificate export failed")
        save_public_candidates(
            state, result.stdout, peer.peer_der, own_public, peer.peer_public_pem
        )
        progress("public_certificates_saved")
        channel = PairingChannel(peer, peer)
        await authenticate_peer(
            channel, code[-5:].encode("ascii"), peer.peer_public_pem, own_public
        )
        await channel.send("contact_info", {"name": name})
        progress("contact_sent")
        contact = await channel.receive("contact_info")
        progress("contact_received")
        # Saving trust is permitted only after both proofs matched, bound to
        # the peer TLS public key and our private app identity.
        from danfoss_mdg.readonly_transport import certificate_peer_id

        record = {
            "format": 1,
            "peer_name": certificate_peer_id(peer.peer_der),
            "display_name": contact.get("name", "Danfoss Link CC"),
            "certificate_der": base64.b64encode(peer.peer_der).decode("ascii"),
        }
        with tempfile.NamedTemporaryFile(mode="w", dir=state, delete=False) as f:
            temporary = Path(f.name)
            os.fchmod(f.fileno(), 0o600)
            json.dump(record, f)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(temporary, state / "paired_cc.json")
        finally:
            temporary.unlink(missing_ok=True)
        print("PACE verified; CC paired and private trust record saved", flush=True)
        progress("trust_saved")
    finally:
        if peer:
            peer.close()
            try:
                await asyncio.wait_for(peer.wait_closed(), 3)
            except (OSError, TimeoutError):
                pass
        if control:
            await control.close()


def main():
    parser = argparse.ArgumentParser(
        description="Pair own CC using hidden terminal input"
    )
    parser.add_argument(
        "--diagnose-code-format",
        action="store_true",
        help="Classify hidden code without network or identity changes",
    )
    parser.add_argument(
        "--apk-identity-test",
        action="store_true",
        help="Use a separate APK-profile identity below the state directory",
    )
    parser.add_argument("--state", type=Path, default=Path("/state"))
    parser.add_argument(
        "--name", default="Danfoss MQTT", help="Display name in CC app list"
    )
    args = parser.parse_args()
    if not 1 <= len(args.name) <= 64 or not args.name.isprintable():
        print("Invalid client display name", file=sys.stderr)
        return 1
    if not ssl.OPENSSL_VERSION.startswith("OpenSSL 3.5.9 "):
        print("Run inside the isolated private MDG TLS runtime", file=sys.stderr)
        return 1
    if not sys.stdin.isatty():
        print(
            "An interactive terminal is required for hidden code entry", file=sys.stderr
        )
        return 1
    if args.apk_identity_test and not args.diagnose_code_format:
        args.state = args.state / "apk-profile"
    if (args.state / "paired_cc.json").exists():
        print("Already paired; refusing to overwrite CC trust", file=sys.stderr)
        return 1
    try:
        code = normalize_code(getpass.getpass("CC pairing code (hidden): "))
    except PairingError:
        print("Invalid pairing code format", file=sys.stderr)
        return 1
    if args.diagnose_code_format:
        print(
            "Local pairing code detected"
            if code_mode(code) == "local"
            else "Cloud pairing code detected"
        )
        return 0
    try:
        asyncio.run(
            pair(
                args.state,
                code,
                args.name,
                identity_profile="apk" if args.apk_identity_test else "legacy",
            )
        )
        return 0
    except (ControlError, PairingError, IdentityError) as error:
        progress("pair_failed")
        print(str(error), file=sys.stderr)
        return 1
    except (TimeoutError, OSError, ValueError, KeyError):
        progress("pair_failed")
        print("Pairing failed; no new peer trust saved", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
