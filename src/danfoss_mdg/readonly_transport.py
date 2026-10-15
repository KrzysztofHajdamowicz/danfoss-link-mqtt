"""Paired, certificate-pinned cc_json transport exposing only allowlisted GETs."""

import asyncio, base64, json, os, stat, re, subprocess
from pathlib import Path
from .control import ControlChannel, ControlError, HOSTS
from .routing import endpoint, negotiate, PeerStream
from .framing import read_frame, send_frame
from .pairing import PairingError
from danfoss_readonly import read_request, ProtocolError


def certificate_peer_id(der):
    # APK ssl.x509.subject_key_identifier emits lower-case colon hex.
    result = subprocess.run(
        ["openssl", "x509", "-inform", "DER", "-noout", "-ext", "subjectKeyIdentifier"],
        input=der,
        capture_output=True,
        timeout=10,
    )
    match = re.search(
        rb"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{2}:){19}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:])",
        result.stdout,
    )
    if result.returncode or match is None:
        raise ProtocolError("Saved certificate has no valid peer identifier")
    return match.group().decode("ascii").lower()


def load_trust(state):
    state = Path(state)
    for path in (
        state,
        state / "app_key.pem",
        state / "app_cert.pem",
        state / "paired_cc.json",
    ):
        info = path.lstat()
        if (
            path.is_symlink()
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or not (
                stat.S_ISDIR(info.st_mode)
                if path == state
                else stat.S_ISREG(info.st_mode)
            )
        ):
            raise ProtocolError("Invalid private paired state")
    record_path = state / "paired_cc.json"
    if record_path.stat().st_size > 65535:
        raise ProtocolError("Invalid saved trust size")
    record = json.loads(record_path.read_text())
    if record.get("format") != 1:
        raise ProtocolError("Unsupported saved trust format")
    der = base64.b64decode(record["certificate_der"], validate=True)
    if not 1 <= len(der) <= 65535:
        raise ProtocolError("Invalid saved certificate")
    return state / "app_key.pem", state / "app_cert.pem", certificate_peer_id(der), der


class PairedReadTransport:
    def __init__(self, control, peer):
        self.control = control
        self.peer = peer
        self.lock = asyncio.Lock()

    @classmethod
    async def connect(cls, state):
        key, cert, peer_name, der = load_trust(state)
        control = peer = writer = None
        try:
            for host in HOSTS:
                try:
                    control = await ControlChannel.connect(key, cert, host)
                    break
                except ControlError:
                    pass
            if control is None:
                raise ProtocolError("Paired grid connection unavailable")
            route = await control.place_read_call(peer_name)
            host, port, token = endpoint(route)
            async with asyncio.timeout(15):
                reader, writer = await asyncio.open_connection(host, port)
            await negotiate(reader, writer, token)
            peer = PeerStream(reader, writer, key, cert, trusted_peer_der=der)
            await peer.handshake()
            return cls(control, peer)
        except Exception:
            if writer:
                writer.close()
            if control:
                await control.close()
            raise

    async def exchange(self, request):
        # Exact reconstruction prevents callers injecting writes, headers or extra fields.
        try:
            expected = read_request(request["id"], request["data"]["data"]["uri"])
        except (KeyError, TypeError, ValueError):
            raise ProtocolError("Read request rejected") from None
        if request != expected:
            raise ProtocolError("Read request rejected")
        return await self._exchange_checked(request)

    async def _exchange_checked(self, request):
        async with self.lock:
            await send_frame(
                self.peer, json.dumps(request, separators=(",", ":")).encode()
            )
            data = await read_frame(self.peer, 30)
            try:
                response = json.loads(data)
            except (ValueError, UnicodeError):
                raise ProtocolError("Invalid CC JSON response") from None
            if not isinstance(response, dict):
                raise ProtocolError("Invalid CC JSON response")
            return response

    async def close(self):
        self.peer.close()
        try:
            await asyncio.wait_for(self.peer.wait_closed(), 3)
        except (OSError, TimeoutError):
            pass
        await self.control.close()
