"""Regressions for private OpenSSL build. All TLS fixtures use memory BIOs."""

import ssl
import asyncio
from pathlib import Path
import subprocess
import tempfile
import unittest
from danfoss_mdg.peer_certificate import candidate_public_key
from danfoss_mdg.pairing import PairingError
from danfoss_mdg.routing import PeerStream


class NativeTLSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.ca_key = cls.root / "ca.key"
        cls.ca = cls.root / "ca.pem"
        cls.files = {}
        cls.run_ssl(
            "ecparam",
            "-name",
            "brainpoolP320r1",
            "-param_enc",
            "explicit",
            "-genkey",
            "-noout",
            "-out",
            str(cls.ca_key),
        )
        cls.run_ssl(
            "req",
            "-new",
            "-x509",
            "-key",
            str(cls.ca_key),
            "-sha256",
            "-days",
            "2",
            "-subj",
            "/CN=Synthetic MDG CA",
            "-out",
            str(cls.ca),
        )
        ext = cls.root / "ext.txt"
        ext.write_text(
            "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth,clientAuth\n"
        )
        for serial, (name, curve, encoding) in enumerate(
            [
                ("bp320", "brainpoolP320r1", "explicit"),
                ("named256", "prime256v1", "named_curve"),
                ("explicit256", "prime256v1", "explicit"),
                ("expired", "brainpoolP320r1", "explicit"),
            ],
            2,
        ):
            key = cls.root / (name + ".key")
            csr = cls.root / (name + ".csr")
            cert = cls.root / (name + ".pem")
            cls.run_ssl(
                "ecparam",
                "-name",
                curve,
                "-param_enc",
                encoding,
                "-genkey",
                "-noout",
                "-out",
                str(key),
            )
            cls.run_ssl(
                "req",
                "-new",
                "-key",
                str(key),
                "-subj",
                "/CN=Synthetic " + name,
                "-out",
                str(csr),
            )
            if name == "expired":
                (cls.root / "index.txt").write_text("")
                (cls.root / "serial").write_text("10\n")
                (cls.root / "newcerts").mkdir()
                config = cls.root / "ca.cnf"
                config.write_text(f"""[ca]
default_ca=local
[local]
database={cls.root}/index.txt
serial={cls.root}/serial
new_certs_dir={cls.root}/newcerts
certificate={cls.ca}
private_key={cls.ca_key}
default_md=sha256
policy=policy
x509_extensions=leaf
[policy]
commonName=supplied
[leaf]
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=serverAuth,clientAuth
""")
                cls.run_ssl(
                    "ca",
                    "-batch",
                    "-config",
                    str(config),
                    "-in",
                    str(csr),
                    "-out",
                    str(cert),
                    "-notext",
                    "-startdate",
                    "20200101000000Z",
                    "-enddate",
                    "20200102000000Z",
                )
            else:
                cls.run_ssl(
                    "x509",
                    "-req",
                    "-in",
                    str(csr),
                    "-CA",
                    str(cls.ca),
                    "-CAkey",
                    str(cls.ca_key),
                    "-set_serial",
                    str(serial),
                    "-days",
                    "1",
                    "-sha256",
                    "-extfile",
                    str(ext),
                    "-out",
                    str(cert),
                )
            cls.files[name] = (key, cert)
        der = bytearray(ssl.PEM_cert_to_DER_cert(cls.files["bp320"][1].read_text()))
        der[-1] ^= 1
        bad = cls.root / "bad-signature.pem"
        bad.write_text(ssl.DER_cert_to_PEM_cert(bytes(der)))
        cls.files["bad-signature"] = (cls.files["bp320"][0], bad)

    @classmethod
    def run_ssl(cls, *arguments):
        result = subprocess.run(
            ["openssl", *arguments], capture_output=True, timeout=20
        )
        if result.returncode:
            raise RuntimeError("Native synthetic fixture generation failed")

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def handshake(self, name="bp320", *, trusted=True, mutual=False):
        client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        client.check_hostname = False
        client.verify_mode = ssl.CERT_REQUIRED
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        for context in (client, server):
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.maximum_version = ssl.TLSVersion.TLSv1_2
            context.set_ciphers(
                "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-ECDSA-AES128-GCM-SHA256"
            )
        key, cert = self.files[name]
        server.load_cert_chain(cert, key)
        if trusted:
            client.load_verify_locations(cafile=str(self.ca))
        if mutual:
            key, cert = self.files["bp320"]
            client.load_cert_chain(cert, key)
            server.verify_mode = ssl.CERT_REQUIRED
            server.load_verify_locations(cafile=str(self.ca))
        c_in = ssl.MemoryBIO()
        c_out = ssl.MemoryBIO()
        s_in = ssl.MemoryBIO()
        s_out = ssl.MemoryBIO()
        c = client.wrap_bio(c_in, c_out, server_side=False)
        s = server.wrap_bio(s_in, s_out, server_side=True)
        done = set()
        for _ in range(30):
            for side, connection, out, target in [
                ("client", c, c_out, s_in),
                ("server", s, s_out, c_in),
            ]:
                if side not in done:
                    try:
                        connection.do_handshake()
                        done.add(side)
                    except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                        pass
                    except ssl.SSLCertVerificationError as error:
                        return False, error.verify_code
                    except ssl.SSLError as error:
                        return False, error.reason
                data = out.read()
                if data:
                    target.write(data)
            if len(done) == 2:
                self.assertEqual(c.version(), "TLSv1.2")
                self.assertIsNotNone(c.getpeercert(binary_form=True))
                c.write(b"synthetic encrypted payload")
                s_in.write(c_out.read())
                self.assertEqual(s.read(128), b"synthetic encrypted payload")
                return True, None
        self.fail("Synthetic native TLS handshake did not finish")

    def test_private_runtime_version(self):
        self.assertTrue(
            ssl.OPENSSL_VERSION.startswith("OpenSSL 3.5.9 "), ssl.OPENSSL_VERSION
        )

    def test_pace_selfsigned_candidate_public_key(self):
        der = ssl.PEM_cert_to_DER_cert(self.ca.read_text())
        self.assertTrue(
            candidate_public_key(der).startswith(b"-----BEGIN PUBLIC KEY-----")
        )

    def test_pace_selfsignature_with_distinct_issuer_name(self):
        key = self.files["bp320"][0]
        issuer = self.root / "same-key-issuer.pem"
        leaf = self.root / "same-key-leaf.pem"
        self.run_ssl(
            "req",
            "-new",
            "-x509",
            "-key",
            str(key),
            "-sha256",
            "-days",
            "1",
            "-subj",
            "/CN=Synthetic alternate issuer",
            "-out",
            str(issuer),
        )
        self.run_ssl(
            "x509",
            "-req",
            "-in",
            str(self.root / "bp320.csr"),
            "-CA",
            str(issuer),
            "-CAkey",
            str(key),
            "-set_serial",
            "30",
            "-days",
            "1",
            "-sha256",
            "-extfile",
            str(self.root / "ext.txt"),
            "-out",
            str(leaf),
        )
        der = ssl.PEM_cert_to_DER_cert(leaf.read_text())
        self.assertTrue(
            candidate_public_key(der).startswith(b"-----BEGIN PUBLIC KEY-----")
        )
        corrupted = bytearray(der)
        corrupted[-1] ^= 1
        with self.assertRaises(PairingError):
            candidate_public_key(bytes(corrupted))

    def test_pace_candidate_invalid_signature_rejected(self):
        der = bytearray(ssl.PEM_cert_to_DER_cert(self.ca.read_text()))
        der[-1] ^= 1
        with self.assertRaises(PairingError):
            candidate_public_key(bytes(der))

    def test_pace_candidate_expired_rejected(self):
        der = ssl.PEM_cert_to_DER_cert(self.files["expired"][1].read_text())
        with self.assertRaises(PairingError):
            candidate_public_key(der)

    def test_pace_ca_issued_candidate_accepted(self):
        der = ssl.PEM_cert_to_DER_cert(self.files["bp320"][1].read_text())
        self.assertTrue(
            candidate_public_key(der, self.ca).startswith(b"-----BEGIN PUBLIC KEY-----")
        )

    def test_pinned_issued_certificate_does_not_poison_tls_error_queue(self):
        import ctypes, ctypes.util

        der = ssl.PEM_cert_to_DER_cert(self.files["bp320"][1].read_text())
        lib = ctypes.CDLL(ctypes.util.find_library("crypto"))
        lib.ERR_peek_error.restype = ctypes.c_ulong
        lib.ERR_clear_error()
        self.assertTrue(
            candidate_public_key(der, trusted_der=der).startswith(
                b"-----BEGIN PUBLIC KEY-----"
            )
        )
        self.assertEqual(lib.ERR_peek_error(), 0)
        changed = bytearray(der)
        changed[-1] ^= 1
        with self.assertRaises(PairingError):
            candidate_public_key(bytes(changed), trusted_der=der)

    def test_pace_ca_issued_unknown_issuer_rejected(self):
        der = ssl.PEM_cert_to_DER_cert(self.files["bp320"][1].read_text())
        with self.assertRaises(PairingError):
            candidate_public_key(der)

    def test_explicit_pace_bootstrap_only_allows_missing_issuer(self):
        der = ssl.PEM_cert_to_DER_cert(self.files["bp320"][1].read_text())
        # Default stays strict; bootstrap yields only an untrusted public key.
        with self.assertRaises(PairingError):
            candidate_public_key(der)
        self.assertTrue(
            candidate_public_key(der, allow_missing_issuer=True).startswith(
                b"-----BEGIN PUBLIC KEY-----"
            )
        )
        for name in ("expired", "explicit256", "bad-signature"):
            bad = ssl.PEM_cert_to_DER_cert(self.files[name][1].read_text())
            with self.assertRaises(PairingError):
                candidate_public_key(bad, self.ca, allow_missing_issuer=True)

    def test_pairing_only_pace_bootstrap_enabled(self):
        from danfoss_mdg.routing import ALLOW_MISSING_ISSUER_DURING_PACE

        self.assertTrue(ALLOW_MISSING_ISSUER_DURING_PACE)

    def test_pace_ca_issued_bad_signature_rejected(self):
        der = ssl.PEM_cert_to_DER_cert(self.files["bad-signature"][1].read_text())
        with self.assertRaises(PairingError):
            candidate_public_key(der, self.ca)

    def test_pace_intermediate_chain_is_used_but_not_trusted(self):
        key = self.root / "intermediate.key"
        csr = self.root / "intermediate.csr"
        cert = self.root / "intermediate.pem"
        ext = self.root / "intermediate.ext"
        self.run_ssl(
            "ecparam", "-name", "prime256v1", "-genkey", "-noout", "-out", str(key)
        )
        self.run_ssl(
            "req",
            "-new",
            "-key",
            str(key),
            "-subj",
            "/CN=Synthetic intermediate",
            "-out",
            str(csr),
        )
        ext.write_text(
            "basicConstraints=critical,CA:TRUE,pathlen:0\nkeyUsage=critical,keyCertSign,cRLSign\n"
        )
        self.run_ssl(
            "x509",
            "-req",
            "-in",
            str(csr),
            "-CA",
            str(self.ca),
            "-CAkey",
            str(self.ca_key),
            "-set_serial",
            "20",
            "-days",
            "1",
            "-sha256",
            "-extfile",
            str(ext),
            "-out",
            str(cert),
        )
        leaf = self.root / "intermediate-leaf.pem"
        self.run_ssl(
            "x509",
            "-req",
            "-in",
            str(self.root / "bp320.csr"),
            "-CA",
            str(cert),
            "-CAkey",
            str(key),
            "-set_serial",
            "21",
            "-days",
            "1",
            "-sha256",
            "-extfile",
            str(self.root / "ext.txt"),
            "-out",
            str(leaf),
        )
        der = ssl.PEM_cert_to_DER_cert(leaf.read_text())
        intermediate = ssl.PEM_cert_to_DER_cert(cert.read_text())
        root = ssl.PEM_cert_to_DER_cert(self.ca.read_text())
        with self.assertRaises(PairingError):
            candidate_public_key(der, self.ca)
        self.assertTrue(
            candidate_public_key(der, self.ca, [intermediate, root]).startswith(
                b"-----BEGIN PUBLIC KEY-----"
            )
        )
        with self.assertRaises(PairingError):
            candidate_public_key(der, chain=[intermediate, root])
        corrupted = bytearray(intermediate)
        corrupted[-1] ^= 1
        with self.assertRaises(PairingError):
            candidate_public_key(der, self.ca, [bytes(corrupted), root])

    def test_peer_tls_stream_candidate_and_encrypted_echo(self):
        async def run():
            client_reader = asyncio.StreamReader()
            server_reader = asyncio.StreamReader()

            class Pipe:
                def write(self, data):
                    server_reader.feed_data(data)

                async def drain(self):
                    await asyncio.sleep(0)

            stream = PeerStream(client_reader, Pipe(), self.ca_key, self.ca)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = context.maximum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(self.ca, self.ca_key)
            incoming = ssl.MemoryBIO()
            outgoing = ssl.MemoryBIO()
            server = context.wrap_bio(incoming, outgoing, server_side=True)

            async def fixture():
                while True:
                    try:
                        server.do_handshake()
                        break
                    except ssl.SSLWantReadError:
                        data = outgoing.read()
                        if data:
                            client_reader.feed_data(data)
                        incoming.write(await server_reader.read(65536))
                data = outgoing.read()
                if data:
                    client_reader.feed_data(data)
                while True:
                    try:
                        plaintext = server.read(1024)
                        break
                    except ssl.SSLWantReadError:
                        incoming.write(await server_reader.read(65536))
                self.assertEqual(plaintext, b"synthetic peer payload")
                server.write(plaintext)
                client_reader.feed_data(outgoing.read())

            task = asyncio.create_task(fixture())
            try:
                async with asyncio.timeout(5):
                    await stream.handshake()
                    self.assertGreaterEqual(stream.connection.cipher()[2], 256)
                    self.assertTrue(
                        stream.peer_public_pem.startswith(b"-----BEGIN PUBLIC KEY-----")
                    )
                    stream.write(b"synthetic peer payload")
                    await stream.drain()
                    self.assertEqual(
                        await stream.readexactly(22), b"synthetic peer payload"
                    )
                    await task
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        asyncio.run(run())

    def test_known_explicit_bp320_handshake_and_encrypted_payload(self):
        self.assertEqual(self.handshake(), (True, None))

    def test_bp320_mutual_authentication(self):
        self.assertEqual(self.handshake(mutual=True), (True, None))

    def test_normal_named_curve_still_works(self):
        self.assertEqual(self.handshake("named256"), (True, None))

    def test_other_explicit_curve_rejected(self):
        self.assertEqual(self.handshake("explicit256"), (False, 94))

    def test_unknown_ca_rejected(self):
        success, code = self.handshake(trusted=False)
        self.assertFalse(success)
        self.assertIn(code, (18, 19, 20, 21))

    def test_expired_certificate_rejected(self):
        self.assertEqual(self.handshake("expired"), (False, 10))

    def test_invalid_signature_rejected(self):
        self.assertEqual(self.handshake("bad-signature"), (False, 7))


if __name__ == "__main__":
    unittest.main()
