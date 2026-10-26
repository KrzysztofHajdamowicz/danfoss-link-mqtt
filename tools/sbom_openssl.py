"""Record the private, patched OpenSSL build in a CycloneDX 1.6 SBOM.

syft sees /opt/mdg-openssl/bin/openssl only as a generic binary; this adds the
verified source archive and the brainpool patch as CycloneDX pedigree.
"""

import argparse, importlib.util, json, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATCH = ROOT / "patches" / "openssl-3.5.9-bp320.patch"


def source_pin():
    spec = importlib.util.spec_from_file_location("fetch_openssl", ROOT / "tools" / "fetch_openssl.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.VERSION, module.SHA256, module.URL


def annotate(sbom, version, sha256, url, patch):
    if sbom.get("bomFormat") != "CycloneDX" or sbom.get("specVersion") != "1.6":
        raise SystemExit("Expected a CycloneDX 1.6 SBOM")
    components = sbom.setdefault("components", [])
    purl = "pkg:generic/openssl@" + version
    found = [
        c
        for c in components
        if c.get("name") == "openssl" and str(c.get("purl", "")).startswith("pkg:generic/openssl@")
    ]
    if any(c.get("version") != version for c in found):
        raise SystemExit("SBOM OpenSSL version differs from tools/fetch_openssl.py")
    if not found:
        found = [{"bom-ref": "mdg-openssl-" + str(uuid.uuid4()), "type": "library"}]
        components.append(found[0])
    for component in found:
        component.update(
            name="openssl",
            version=version,
            purl=purl,
            description="Private OpenSSL in /opt/mdg-openssl with a brainpoolP320r1 "
            "compatibility patch; loaded by Python via LD_LIBRARY_PATH",
            licenses=[{"license": {"id": "Apache-2.0"}}],
            externalReferences=[{"type": "distribution", "url": url}],
            pedigree={
                "ancestors": [
                    {
                        "type": "library",
                        "name": "openssl",
                        "version": version,
                        "purl": purl,
                        "hashes": [{"alg": "SHA-256", "content": sha256}],
                    }
                ],
                "patches": [
                    {
                        "type": "unofficial",
                        "diff": {
                            "text": {"contentType": "text/x-diff", "content": patch},
                            "url": "patches/" + PATCH.name,
                        },
                    }
                ],
            },
        )
    return sbom


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sbom", type=Path)
    args = parser.parse_args()
    version, sha256, url = source_pin()
    sbom = annotate(json.loads(args.sbom.read_text()), version, sha256, url, PATCH.read_text())
    args.sbom.write_text(json.dumps(sbom, indent=2) + "\n")


if __name__ == "__main__":
    main()
