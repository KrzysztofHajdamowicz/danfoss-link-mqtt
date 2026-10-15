import unittest, tempfile, subprocess
from pathlib import Path
from danfoss_mdg.identity import ensure_identity, IdentityError


class IdentityProfileTests(unittest.TestCase):
    def test_apk_public_profile_and_preserved_parent_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            oldkey, oldcert = ensure_identity(parent)
            before = (oldkey.read_bytes(), oldcert.read_bytes())
            key, cert = ensure_identity(parent / "apk-profile", profile="apk")
            result = subprocess.run(
                ["openssl", "x509", "-in", str(cert), "-text", "-noout"],
                capture_output=True,
                check=True,
            ).stdout
            for expected in (
                b"Trifork",
                b"localhost",
                b"CA:FALSE",
                b"Authority Key Identifier",
                b"Basic Constraints: critical",
            ):
                self.assertIn(expected, result)
            self.assertIn(b"1 (0x1)", result)
            self.assertEqual(before, (oldkey.read_bytes(), oldcert.read_bytes()))
            self.assertEqual(
                ensure_identity(parent / "apk-profile", profile="apk"), (key, cert)
            )
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            self.assertEqual(cert.stat().st_mode & 0o777, 0o600)
            verify = subprocess.run(
                ["openssl", "verify", "-CAfile", str(cert), str(cert)],
                capture_output=True,
            )
            self.assertEqual(verify.returncode, 0)

    def test_invalid_profile_creates_no_state(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "unused"
            with self.assertRaises(IdentityError):
                ensure_identity(target, profile="invalid")
            self.assertFalse(target.exists())
