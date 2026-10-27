import contextlib, importlib.util, io, re, shutil, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_metadata", ROOT / "tools" / "release_metadata.py")
release_metadata = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_metadata)

NOTES = """<!-- Release notes generated using configuration in .github/release.yml at main -->

## What's Changed
### Changes
* Pair from the add-on configuration by @someone in https://github.com/o/r/pull/5
### Dependencies
* Bump amqtt by @dependabot[bot] in https://github.com/o/r/pull/3


**Full Changelog**: https://github.com/o/r/compare/v0.2.0...v0.3.0
"""


class ReleaseMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for path, *_ in release_metadata.FIELDS + ((release_metadata.CHANGELOG,),):
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(ROOT / path, self.root / path)
        self.version = release_metadata.current(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def run_tool(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            release_metadata.main(list(argv), root=self.root)
        return out.getvalue()

    def bumped(self):
        major, minor, _ = release_metadata.parse(self.version)
        return f"{major}.{minor + 1}.0"

    def test_check_accepts_current_and_newer_and_refuses_older_or_malformed(self):
        self.run_tool("check", self.version)
        self.run_tool("check", self.bumped())
        for version in ("0.0.1", "v1.0.0", "1.0", "01.0.0"):
            with self.subTest(version=version), self.assertRaises(SystemExit):
                self.run_tool("check", version)

    def test_apply_sets_every_version_and_adds_the_changelog_entry(self):
        version = self.bumped()
        notes = self.root / "notes.md"
        notes.write_text(NOTES)
        out = self.run_tool("apply", version, "--notes", str(notes))
        self.assertEqual(len(out.split()), 5)
        for path, pattern, _ in release_metadata.FIELDS:
            self.assertEqual(re.search(pattern, (self.root / path).read_text(), re.M).group(1), version)
        changelog = (self.root / release_metadata.CHANGELOG).read_text()
        self.assertTrue(changelog.startswith(f"# Changelog\n\n## {version}\n\n### Changes\n* Pair"))
        self.assertIn("\n**Full Changelog**: https://github.com/o/r/compare/v0.2.0...v0.3.0\n\n## ", changelog)
        self.assertNotIn("What's Changed", changelog)
        self.assertNotIn("<!--", changelog)
        self.assertIn(f"\n## {self.version}\n", changelog)
        self.assertEqual(self.run_tool("apply", version, "--notes", str(notes)).strip(), "Metadata already up to date")

    def test_existing_entry_for_the_current_version_is_kept(self):
        before = (self.root / release_metadata.CHANGELOG).read_text()
        self.assertEqual(self.run_tool("apply", self.version).strip(), "Metadata already up to date")
        self.assertEqual((self.root / release_metadata.CHANGELOG).read_text(), before)


if __name__ == "__main__":
    unittest.main()
