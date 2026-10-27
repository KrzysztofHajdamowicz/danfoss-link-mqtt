import contextlib, io, os, stat, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from addon_run import bridge_env, import_identity, load_options
from mqtt_bridge import Settings

SERVICE = {"host": "core-mosquitto", "port": 1883, "username": "addons", "password": "synthetic-secret", "ssl": False}


class OptionsTests(unittest.TestCase):
    def settings(self, env):
        with patch.dict(os.environ, env, clear=True):
            return Settings.from_env()

    def test_supervisor_broker_and_defaults(self):
        env = bridge_env({"poll_interval": 60, "allow_writes": True}, SERVICE, base={})
        config = self.settings(env)
        self.assertEqual((config.host, config.port, config.username), ("core-mosquitto", 1883, "addons"))
        self.assertEqual((config.prefix, config.discovery_prefix), ("danfoss_link", "homeassistant"))
        self.assertTrue(config.allow_writes)
        self.assertEqual(env["DANFOSS_SUGGEST_AREAS"], "true")
        self.assertNotIn("DANFOSS_TIMEZONE", env)

    def test_manual_broker_wins_and_readonly(self):
        options = {
            "mqtt_host": "broker.lan",
            "mqtt_tls": True,
            "allow_writes": False,
            "suggest_areas": False,
            "topic_prefix": "heating",
            "timezone": "Europe/Warsaw",
        }
        env = bridge_env(options, SERVICE, base={})
        config = self.settings(env)
        self.assertEqual((config.host, config.port, config.tls), ("broker.lan", 8883, True))
        self.assertEqual(config.username, "")
        self.assertFalse(config.allow_writes)
        self.assertEqual(config.prefix, "heating")
        self.assertEqual((env["DANFOSS_SUGGEST_AREAS"], env["DANFOSS_TIMEZONE"]), ("false", "Europe/Warsaw"))

    def test_no_broker(self):
        self.assertIsNone(bridge_env({}, None, base={}))

    def test_missing_or_invalid_options_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "options.json"
            self.assertEqual(load_options(path), {})
            path.write_text("[1]")
            self.assertEqual(load_options(path), {})


class IdentityImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.state, self.config = root / "data" / "apk-profile", root / "config"
        self.config.mkdir()
        for name in ("app_key.pem", "app_cert.pem", "paired_cc.json"):
            (self.config / name).write_text("synthetic " + name)

    def tearDown(self):
        self.temp.cleanup()

    def run_import(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return import_identity(self.state, self.config)

    def test_import_is_private_and_never_overwrites(self):
        self.assertTrue(self.run_import())
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        for name in ("app_key.pem", "app_cert.pem", "paired_cc.json"):
            self.assertEqual(stat.S_IMODE((self.state / name).stat().st_mode), 0o600)
        self.assertEqual((self.state / "app_key.pem").read_text(), "synthetic app_key.pem")
        (self.config / "app_key.pem").write_text("other")
        self.assertFalse(self.run_import())
        self.assertEqual((self.state / "app_key.pem").read_text(), "synthetic app_key.pem")

    def test_incomplete_or_symlinked_identity_ignored(self):
        (self.config / "app_cert.pem").unlink()
        self.assertFalse(self.run_import())
        (self.config / "app_cert.pem").symlink_to(self.config / "app_key.pem")
        self.assertFalse(self.run_import())
        self.assertFalse((self.state / "paired_cc.json").exists())


if __name__ == "__main__":
    unittest.main()
