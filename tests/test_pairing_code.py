import contextlib, io, stat, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import addon_run
import pair_cc
from danfoss_mdg.pairing import PairingError

CODE = "123456789"


class PairingCodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "apk-profile"
        self.out = io.StringIO()

    def tearDown(self):
        self.temp.cleanup()

    def paired(self, state, code, name, **_):
        (Path(state) / "paired_cc.json").write_text("{}")
        return True

    def run_pair(self, code, result=False):
        fake = self.paired if result else (lambda *a, **k: False)
        with (
            patch("pair_cc.pair_once", side_effect=fake) as once,
            contextlib.redirect_stdout(self.out),
            contextlib.redirect_stderr(self.out),
        ):
            ok = pair_cc.pair_with_code(self.state, code, "Home Assistant")
        return ok, once

    def test_success_uses_normalized_code_and_drops_the_record(self):
        ok, once = self.run_pair("123-456 789\n", result=True)
        self.assertTrue(ok)
        once.assert_called_once_with(self.state, CODE, "Home Assistant")
        self.assertFalse((self.state / pair_cc.ATTEMPT).exists())
        self.assertNotIn(CODE, self.out.getvalue())

    def test_failed_code_is_never_tried_again(self):
        self.assertFalse(self.run_pair(CODE)[0])
        record = self.state / pair_cc.ATTEMPT
        self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)
        self.assertNotIn(CODE, record.read_text())
        ok, once = self.run_pair("123 456 789")
        self.assertFalse(ok)
        once.assert_not_called()
        self.assertIn("already tried", self.out.getvalue())
        ok, once = self.run_pair("987654321", result=True)
        self.assertTrue(ok)
        once.assert_called_once()
        self.assertNotIn(CODE, self.out.getvalue())

    def test_invalid_and_local_codes_are_refused_before_any_attempt(self):
        for code in ("12345", "abc123456", "000012345"):
            with self.subTest(code=code):
                ok, once = self.run_pair(code)
                self.assertFalse(ok)
                once.assert_not_called()
                self.assertFalse((self.state / pair_cc.ATTEMPT).exists())
                self.assertNotIn(code, self.out.getvalue())

    def test_pair_once_reports_errors_without_the_code(self):
        self.state.mkdir(mode=0o700)
        with (
            patch("pair_cc.pair", side_effect=PairingError("Peer proof mismatch")),
            patch("pair_cc.progress"),
            contextlib.redirect_stderr(self.out),
        ):
            self.assertFalse(pair_cc.pair_once(self.state, CODE, "Danfoss MQTT"))
        self.assertEqual(self.out.getvalue().strip(), "Peer proof mismatch")


class CodeFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.state, self.file = root / "apk-profile", root / "pairing-code.txt"

    def tearDown(self):
        self.temp.cleanup()

    def test_read_code_file(self):
        self.file.write_text(" 123-456-789\n")
        self.assertEqual(pair_cc.read_code_file(self.file), CODE)
        for content in ("1" * 300, "kod"):
            with self.subTest(content=content[:5]):
                self.file.write_text(content)
                with self.assertRaises(PairingError):
                    pair_cc.read_code_file(self.file)
        for path in (Path(self.temp.name), self.file.with_name("missing")):
            with self.assertRaises(PairingError):
                pair_cc.read_code_file(path)

    def test_waits_for_a_new_code_after_a_failure(self):
        self.file.write_text(CODE)
        attempts, ticks = [], []

        def attempt(state, code, name):
            attempts.append(code)
            if code == "987654321":
                (Path(state) / "paired_cc.json").write_text("{}")
                return True
            return False

        def sleep(_):
            ticks.append(None)
            if len(ticks) == 3:
                self.file.write_text("987654321")

        self.state.mkdir(mode=0o700)
        with (
            patch("pair_cc.pair_with_code", side_effect=attempt),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            pair_cc.pair_from_file(self.state, self.file, sleep=sleep)
        self.assertEqual(attempts, [CODE, "987654321"])
        self.assertEqual(len(ticks), 3)


class AddonOptionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.state, self.config = root / "data" / "apk-profile", root / "config"
        self.config.mkdir()
        self.state.mkdir(parents=True, mode=0o700)

    def tearDown(self):
        self.temp.cleanup()

    def wait(self, code, pair):
        out = io.StringIO()
        with patch("pair_cc.pair_with_code", side_effect=pair) as attempt, contextlib.redirect_stdout(out):
            addon_run.wait_until_paired(self.state, self.config, interval=0, code=code)
        return attempt, out.getvalue()

    def test_option_code_pairs_once(self):
        def pair(state, code, name):
            (state / "paired_cc.json").write_text("{}")
            return True

        attempt, out = self.wait(CODE, pair)
        attempt.assert_called_once_with(self.state, CODE, "Home Assistant")
        self.assertIn("remove the pairing code", out)
        self.assertNotIn("NOT PAIRED", out)
        attempt, out = self.wait(CODE, pair)
        attempt.assert_not_called()
        self.assertIn("ignored", out)

    def test_failed_option_falls_back_to_waiting_for_an_identity(self):
        for name in ("app_key.pem", "app_cert.pem", "paired_cc.json"):
            (self.config / name).write_text("synthetic " + name)
        attempt, out = self.wait(CODE, lambda *a: False)
        attempt.assert_called_once()
        self.assertIn("NOT PAIRED", out)
        self.assertTrue((self.state / "paired_cc.json").exists())
        self.assertNotIn(CODE, out)

    def test_main_passes_the_option(self):
        options = {"pairing_code": " 123456789 "}
        with (
            patch("addon_run.load_options", return_value=options),
            patch("addon_run.supervisor_mqtt", return_value={"host": "core-mosquitto"}),
            patch("addon_run.import_identity"),
            patch("addon_run.wait_until_paired") as wait,
            patch("addon_run.os.execve") as execve,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            addon_run.main()
        wait.assert_called_once_with(code=CODE)
        self.assertNotIn(CODE, str(execve.call_args))


if __name__ == "__main__":
    unittest.main()
