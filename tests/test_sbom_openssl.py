import importlib.util, unittest
from pathlib import Path

TOOL = Path(__file__).parents[1] / "tools" / "sbom_openssl.py"
spec = importlib.util.spec_from_file_location("sbom_openssl", TOOL)
sbom_openssl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sbom_openssl)


def sbom(*components):
    return {"bomFormat": "CycloneDX", "specVersion": "1.6", "components": list(components)}


class SbomOpensslTests(unittest.TestCase):
    def setUp(self):
        self.version, self.sha256, self.url = sbom_openssl.source_pin()

    def annotate(self, document):
        return sbom_openssl.annotate(document, self.version, self.sha256, self.url, "--- patch")

    def test_detected_binary_gets_pedigree(self):
        detected = {"bom-ref": "x", "type": "application", "name": "openssl", "version": self.version, "purl": "pkg:generic/openssl@" + self.version}
        result = self.annotate(sbom(detected, {"name": "libssl3", "purl": "pkg:apk/alpine/libssl3@3"}))
        component = result["components"][0]
        self.assertEqual(component["bom-ref"], "x")
        self.assertEqual(component["pedigree"]["ancestors"][0]["hashes"][0]["content"], self.sha256)
        self.assertEqual(component["pedigree"]["patches"][0]["type"], "unofficial")
        self.assertNotIn("pedigree", result["components"][1])

    def test_missing_component_added_and_mismatch_rejected(self):
        result = self.annotate(sbom())
        self.assertEqual(result["components"][0]["purl"], "pkg:generic/openssl@" + self.version)
        stale = {"name": "openssl", "version": "3.0.0", "purl": "pkg:generic/openssl@3.0.0"}
        with self.assertRaises(SystemExit):
            self.annotate(sbom(stale))
        with self.assertRaises(SystemExit):
            self.annotate({"bomFormat": "CycloneDX", "specVersion": "1.5"})


if __name__ == "__main__":
    unittest.main()
