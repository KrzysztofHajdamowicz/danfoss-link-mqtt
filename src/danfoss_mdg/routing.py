"""Authenticated-grid referral, plaintext router negotiation, then peer TLS."""

import asyncio
import ipaddress
import ssl
from .framing import read_frame, send_frame
from .protobuf import packet, decode
from .pairing import PairingError
from .peer_certificate import candidate_public_key
from .diagnostics import progress

# Pairing-only missing-issuer bootstrap is authenticated by panel-code PACE.
# This never changes grid/control TLS; PACE must authenticate before trust.
ALLOW_MISSING_ISSUER_DURING_PACE = True


def endpoint(route):
    try:
        record = route["router"][0]
        host = record["host"].decode("ascii")
        address = ipaddress.ip_address(host)
        port = record["port"]
        token = route["token"]
        if not address.is_global or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ValueError()
        if not isinstance(token, bytes) or not 1 <= len(token) <= 4096:
            raise ValueError()
        return str(address), port, token
    except (KeyError, IndexError, TypeError, AttributeError, UnicodeError, ValueError):
        raise PairingError("Invalid authenticated grid routing referral") from None


async def negotiate(reader, writer, token, timeout=120):
    await send_frame(
        writer,
        packet(
            "mdg_routing_pb",
            ".mdg.routing.Handshake",
            {
                "magic": 4036856981,
                "version_major": 1,
                "version_minor": 0,
                "token": token,
                "supported_protocols": ["tlsv1_2"],
            },
            0,
        ),
    )
    progress("router_hello")
    async with asyncio.timeout(timeout):
        for _ in range(16):
            data = await read_frame(reader, timeout)
            if data[0] == 1:
                decode("mdg_routing_pb", ".mdg.routing.PleaseHold", data[1:])
                progress("router_hold")
            elif data[0] == 2:
                fields = decode("mdg_routing_pb", ".mdg.routing.GoAhead", data[1:])
                if fields.get("selected_protocol") != "tlsv1_2":
                    raise PairingError("Router did not select TLS 1.2")
                progress("router_ready")
                return
            else:
                raise PairingError("Router refused pairing connection")
    raise PairingError("Router negotiation limit reached")


class TLSIdentityDiagnostics:
    """Observe TLS 1.2 identity presentation without retaining handshake bytes."""

    def __init__(self):
        self.request_count = 0
        self.certificate_count = 0

    def observe(self, connection, direction, version, content_type, msg_type, data):
        if int(content_type) != 22:
            return
        if direction == "read" and int(msg_type) == 13:
            self.request_count = min(65535, self.request_count + 1)
        elif direction == "write" and int(msg_type) == 11:
            # TLS 1.2: handshake header, certificate-list length, DER entries.
            if len(data) < 7:
                return
            length = int.from_bytes(data[4:7], "big")
            if length != len(data) - 7:
                return
            offset = 7
            count = 0
            while offset < len(data):
                if offset + 3 > len(data):
                    return
                size = int.from_bytes(data[offset : offset + 3], "big")
                offset += 3
                if not size or offset + size > len(data):
                    return
                offset += size
                count += 1
            self.certificate_count = min(65535, count)


class PeerStream:
    """Memory BIO preserves bytes on the existing router socket for TLS upgrade."""

    def __init__(
        self,
        reader,
        writer,
        key,
        certificate,
        *,
        allow_missing_issuer=False,
        trusted_peer_der=None,
    ):
        self.reader = reader
        self.writer = writer
        self.pending = bytearray()
        self.allow_missing_issuer = allow_missing_issuer
        self.trusted_peer_der = trusted_peer_der
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        # PACE peers are self-signed. This context is only a candidate channel.
        # Self-signature/validity/curve are checked before PACE; OTP proof is
        # required before contact exchange and before saving any peer trust.
        ctx.verify_mode = ssl.CERT_NONE
        ctx.minimum_version = ctx.maximum_version = ssl.TLSVersion.TLSv1_2
        # APK mdg_routing_connection validates cipher bits >=256 after TLS.
        ctx.set_ciphers(
            "ECDHE-ECDSA-AES256-GCM-SHA384:"
            "ECDHE-ECDSA-AES256-SHA384:ECDHE-ECDSA-AES256-SHA"
        )
        ctx.load_cert_chain(str(certificate), str(key))
        self.identity_diagnostics = TLSIdentityDiagnostics()
        ctx._msg_callback = self.identity_diagnostics.observe
        self.incoming = ssl.MemoryBIO()
        self.outgoing = ssl.MemoryBIO()
        self.connection = ctx.wrap_bio(self.incoming, self.outgoing, server_side=False)
        self.peer_der = None
        self.peer_public_pem = None

    async def flush(self):
        data = self.outgoing.read()
        if data:
            self.writer.write(data)
        await self.writer.drain()

    async def feed(self):
        await self.flush()
        data = await self.reader.read(65536)
        if not data:
            raise PairingError("Peer TLS connection closed")
        self.incoming.write(data)

    async def handshake(self):
        try:
            progress("peer_tls_start")
            async with asyncio.timeout(20):
                while True:
                    try:
                        self.connection.do_handshake()
                        break
                    except ssl.SSLWantReadError:
                        await self.feed()
                    except ssl.SSLWantWriteError:
                        await self.flush()
                await self.flush()
            cipher = self.connection.cipher()
            if (
                self.connection.version() != "TLSv1.2"
                or not cipher
                or not cipher[0].startswith("ECDHE-ECDSA-")
                or cipher[2] < 256
            ):
                raise PairingError("Peer TLS did not meet app security parameters")
            progress("peer_tls_ready", cipher=cipher[0], cipher_bits=cipher[2])
            progress(
                "peer_tls_identity",
                certificate_requests=self.identity_diagnostics.request_count,
                client_certificates=self.identity_diagnostics.certificate_count,
            )
            self.peer_der = self.connection.getpeercert(binary_form=True)
            # CPython 3.12 exposes the peer chain on its native SSL object.
            # These certificates are untrusted intermediates, never new roots.
            certificates = self.connection._sslobj.get_unverified_chain() or []
            chain = [ssl.PEM_cert_to_DER_cert(c.public_bytes()) for c in certificates]
            progress("peer_certificate", chain_count=len(chain))
            self.peer_public_pem = candidate_public_key(
                self.peer_der,
                chain=chain,
                allow_missing_issuer=self.allow_missing_issuer,
                trusted_der=self.trusted_peer_der,
            )
        except (ssl.SSLError, OSError, TimeoutError):
            raise PairingError("Peer TLS handshake failed") from None

    async def readexactly(self, count):
        result = bytearray()
        try:
            while len(result) < count:
                try:
                    chunk = self.connection.read(count - len(result))
                    if not chunk:
                        raise PairingError("Peer TLS connection closed")
                    result.extend(chunk)
                except ssl.SSLWantReadError:
                    await self.feed()
                except ssl.SSLWantWriteError:
                    await self.flush()
            return bytes(result)
        except (ssl.SSLError, OSError):
            raise PairingError("Peer TLS read failed") from None

    def write(self, data):
        if len(self.pending) + len(data) > 65537:
            raise PairingError("Peer TLS write too large")
        self.pending.extend(data)

    async def drain(self):
        data = bytes(self.pending)
        offset = 0
        try:
            while offset < len(data):
                try:
                    offset += self.connection.write(data[offset:])
                except ssl.SSLWantReadError:
                    await self.feed()
                except ssl.SSLWantWriteError:
                    await self.flush()
            self.pending.clear()
            await self.flush()
        except (ssl.SSLError, OSError):
            raise PairingError("Peer TLS write failed") from None

    def close(self):
        self.pending.clear()
        self.writer.close()

    async def wait_closed(self):
        await self.writer.wait_closed()


async def connect_pairing_route(route, key, certificate):
    host, port, token = endpoint(route)
    writer = None
    try:
        async with asyncio.timeout(15):
            reader, writer = await asyncio.open_connection(host, port)
        await negotiate(reader, writer, token)
        stream = PeerStream(
            reader,
            writer,
            key,
            certificate,
            allow_missing_issuer=ALLOW_MISSING_ISSUER_DURING_PACE,
        )
        await stream.handshake()
        return stream
    except Exception:
        if writer:
            writer.close()
        raise
