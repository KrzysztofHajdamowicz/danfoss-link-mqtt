import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import pair_cc


class CodeDiagnosticTests(unittest.TestCase):
    def test_format_check_never_connects_or_saves_code(self):
        for code, expected in [
            ("000012345", "Local pairing code detected"),
            ("123456789", "Cloud pairing code detected"),
        ]:
            with self.subTest(mode=expected), tempfile.TemporaryDirectory() as state:
                out = io.StringIO()
                with (
                    patch(
                        "sys.argv",
                        ["pair_cc.py", "--diagnose-code-format", "--state", state],
                    ),
                    patch("pair_cc.ssl.OPENSSL_VERSION", "OpenSSL 3.5.9 fixture"),
                    patch("pair_cc.sys.stdin.isatty", return_value=True),
                    patch("pair_cc.getpass.getpass", return_value=code),
                    patch("pair_cc.pair") as pair,
                    patch("pair_cc.ensure_identity") as identity,
                    contextlib.redirect_stdout(out),
                ):
                    self.assertEqual(pair_cc.main(), 0)
                self.assertEqual(out.getvalue().strip(), expected)
                self.assertNotIn(code, out.getvalue())
                pair.assert_not_called()
                identity.assert_not_called()
                self.assertEqual(list(Path(state).iterdir()), [])

    def test_apk_local_code_cannot_reach_cloud_pairing(self):
        import asyncio

        with patch("pair_cc.ensure_identity") as identity:
            with self.assertRaises(pair_cc.PairingError):
                asyncio.run(pair_cc.pair(Path("/unused"), "000012345"))
            identity.assert_not_called()
