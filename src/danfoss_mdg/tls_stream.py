"""Async memory-BIO TLS adapter with the narrowly scoped MDG certificate policy."""

import asyncio
import re
from pathlib import Path
from OpenSSL import SSL, crypto
from .certificate_policy import CertificatePolicy


class TLSError(OSError):
    pass


def make_context(key, certificate):
    policy = CertificatePolicy()
    context = SSL.Context(SSL.TLS_CLIENT_METHOD)
    context.set_min_proto_version(SSL.TLS1_2_VERSION)
    context.set_max_proto_version(SSL.TLS1_2_VERSION)
    context.set_options(SSL.OP_NO_COMPRESSION)
    context.set_verify(SSL.VERIFY_PEER, policy.verify)
    context.load_verify_locations(str(Path(__file__).with_name("trusted-CAs.pem")))
    context.use_certificate_file(str(certificate))
    context.use_privatekey_file(str(key))
    context.check_privatekey()
    context.set_tmp_ecdh(crypto.get_elliptic_curve("brainpoolP320r1"))
    context.set_cipher_list(
        b"ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES128-SHA256:"
        b"ECDHE-ECDSA-AES128-SHA:ECDHE-ECDSA-AES256-GCM-SHA384:"
        b"ECDHE-ECDSA-AES256-SHA384:ECDHE-ECDSA-AES256-SHA"
    )
    return context, policy


class TLSStream:
    def __init__(self, reader, writer, context, policy):
        self.reader = reader
        self.writer = writer
        self.policy = policy
        self.connection = SSL.Connection(context, None)
        self.connection.set_connect_state()
        self.pending = bytearray()
        self.closed = False

    async def flush(self):
        try:
            while True:
                self.writer.write(self.connection.bio_read(65536))
        except SSL.WantReadError:
            pass
        await self.writer.drain()

    async def feed(self):
        await self.flush()
        data = await self.reader.read(65536)
        if not data:
            raise TLSError("TLS peer closed connection")
        self.connection.bio_write(data)

    async def handshake(self):
        try:
            while True:
                try:
                    self.connection.do_handshake()
                    break
                except SSL.WantReadError:
                    await self.feed()
                except SSL.WantWriteError:
                    await self.flush()
            await self.flush()
            if self.connection.get_peer_certificate() is None:
                raise TLSError("TLS peer certificate absent")
            if self.connection.get_protocol_version_name() != "TLSv1.2":
                raise TLSError("Unexpected TLS protocol")
        except SSL.Error as error:
            # ERR_reason_error_string diagnostics are fixed OpenSSL reason names.
            reasons = []
            if error.args and isinstance(error.args[0], list):
                for entry in error.args[0]:
                    if (
                        isinstance(entry, tuple)
                        and len(entry) == 3
                        and isinstance(entry[2], str)
                        and re.fullmatch(r"[A-Za-z0-9 _-]{1,100}", entry[2])
                    ):
                        reasons.append(entry[2])
            reason = ", ".join(reasons) or "unclassified TLS failure"
            code = self.policy.last_error
            raise TLSError(
                "TLS verification or handshake failed"
                + (f" (OpenSSL code {code})" if code is not None else "")
                + f"; known-curve checks={self.policy.compatibility_checks}; rejected curve={self.policy.rejected_curve or 'none'}; reason={reason}"
            ) from None

    async def readexactly(self, count):
        result = bytearray()
        try:
            while len(result) < count:
                try:
                    chunk = self.connection.recv(count - len(result))
                    if not chunk:
                        raise asyncio.IncompleteReadError(bytes(result), count)
                    result.extend(chunk)
                except SSL.WantReadError:
                    await self.feed()
                except SSL.WantWriteError:
                    await self.flush()
            await self.flush()
            return bytes(result)
        except SSL.ZeroReturnError:
            raise asyncio.IncompleteReadError(bytes(result), count) from None
        except SSL.Error:
            raise TLSError("TLS receive failed") from None

    def write(self, data):
        if self.closed or len(self.pending) + len(data) > 65537:
            raise TLSError("TLS write rejected")
        self.pending.extend(data)

    async def drain(self):
        # Retain the same immutable buffer across WantRead/WantWrite retries.
        data = bytes(self.pending)
        offset = 0
        try:
            while offset < len(data):
                block = data[offset:]
                while True:
                    try:
                        sent = self.connection.send(block)
                        break
                    except SSL.WantReadError:
                        await self.feed()
                    except SSL.WantWriteError:
                        await self.flush()
                if sent <= 0:
                    raise TLSError("TLS send failed")
                offset += sent
            self.pending.clear()
            await self.flush()
        except SSL.Error:
            raise TLSError("TLS send failed") from None

    def close(self):
        self.closed = True
        self.pending.clear()
        self.writer.close()

    async def wait_closed(self):
        await self.writer.wait_closed()


async def open_stream(host, key, certificate, timeout=10):
    stream = None
    writer = None
    try:
        async with asyncio.timeout(timeout):
            context, policy = make_context(key, certificate)
            reader, writer = await asyncio.open_connection(host, 443)
            stream = TLSStream(reader, writer, context, policy)
            await stream.handshake()
            return stream
    except Exception:
        if writer:
            writer.close()
        raise
