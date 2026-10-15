"""Fetch official OpenSSL source and verify its digest before extracting it."""

import hashlib
from pathlib import Path
import tarfile
import urllib.request

VERSION = "3.5.9"
SHA256 = "603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a"
URL = f"https://github.com/openssl/openssl/releases/download/openssl-{VERSION}/openssl-{VERSION}.tar.gz"


def main():
    archive = Path("source.tar.gz")
    urllib.request.urlretrieve(URL, archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
        raise RuntimeError("OpenSSL source checksum mismatch")
    with tarfile.open(archive) as source:
        source.extractall(Path.cwd(), filter="data")
    archive.unlink()


if __name__ == "__main__":
    main()
