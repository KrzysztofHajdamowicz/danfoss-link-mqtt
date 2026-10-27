"""Pairing of owner's CC: hidden prompt, add-on option or code file.

Never place the code in argv, env or logs.
"""

import argparse
import asyncio
import base64
import getpass
import hashlib
import json
import os
from pathlib import Path
import ssl
import stat
import subprocess
import sys
import tempfile
import time
from danfoss_mdg.control import ControlChannel, ControlError, HOSTS
from danfoss_mdg.identity import ensure_identity, IdentityError, private_directory
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


def pair_once(state, code, name, *, identity_profile="apk"):
    """Run one pairing attempt; True once trust is saved. Errors are printed without the code."""
    try:
        asyncio.run(pair(state, code, name, identity_profile=identity_profile))
        return True
    except (ControlError, PairingError, IdentityError) as error:
        progress("pair_failed")
        print(str(error), file=sys.stderr, flush=True)
    except (TimeoutError, OSError, ValueError, KeyError):
        progress("pair_failed")
        print("Pairing failed; no new peer trust saved", file=sys.stderr, flush=True)
    return False


ATTEMPT = "pairing_attempt"


def _fingerprint(salt, code):
    return hashlib.sha256(salt + code.encode("ascii")).hexdigest()


def _already_tried(record, code):
    try:
        salt, digest = record.read_text().split()
        return _fingerprint(bytes.fromhex(salt), code) == digest
    except (OSError, ValueError):
        return False


def pair_with_code(state, code, name):
    """Pair without a terminal (add-on option, code file); True once trust is saved.

    A configured code stays in place across restarts, so a salted fingerprint of
    each attempted code is kept and the same code is never tried twice.
    """
    try:
        code = normalize_code(code)
        if code_mode(code) == "local":
            raise PairingError("Local pairing code detected; cloud pairing was not attempted")
        state = private_directory(state)
    except (PairingError, IdentityError) as error:
        print(str(error), file=sys.stderr, flush=True)
        return False
    record = state / ATTEMPT
    if _already_tried(record, code):
        print(
            "This pairing code was already tried; generate a new one on the CC panel",
            file=sys.stderr,
            flush=True,
        )
        return False
    salt = os.urandom(16)
    with tempfile.NamedTemporaryFile(mode="w", dir=state, delete=False) as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(salt.hex() + " " + _fingerprint(salt, code) + "\n")
    os.replace(f.name, record)
    print("Pairing with the configured code", flush=True)
    if not pair_once(state, code, name):
        return False
    record.unlink(missing_ok=True)
    return True


def read_code_file(path):
    """Read a code from a small regular file such as a Docker secret."""
    try:
        with open(path, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 256:
                raise PairingError("Pairing code file must be a small regular file")
            data = file.read(257)
    except OSError:
        raise PairingError("Pairing code file is unreadable") from None
    try:
        return normalize_code(data.decode("ascii"))
    except UnicodeDecodeError:
        raise PairingError("Invalid pairing code format") from None


def pair_from_file(state, path, name="Danfoss MQTT", *, interval=30, sleep=time.sleep):
    """Pair from a code file, waiting for a new code after a failed attempt."""
    seen = None
    while not (Path(state) / "paired_cc.json").exists():
        try:
            current = read_code_file(path)
        except PairingError as error:
            current = str(error)
            if current != seen:
                print(current, file=sys.stderr, flush=True)
        else:
            if current != seen:
                if pair_with_code(state, current, name):
                    return
                print("Waiting for a new code in the pairing code file", flush=True)
        seen = current
        sleep(interval)


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
    profile = "apk" if args.apk_identity_test else "legacy"
    return 0 if pair_once(args.state, code, args.name, identity_profile=profile) else 1


if __name__ == "__main__":
    sys.exit(main())
