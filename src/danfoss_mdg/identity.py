"""Generate a new app identity. Never import an existing phone's private key."""

import os
from pathlib import Path
import stat
import subprocess
import tempfile


class IdentityError(Exception):
    pass


def _openssl(arguments):
    try:
        result = subprocess.run(
            ["openssl", *arguments], capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        raise IdentityError("OpenSSL could not run") from None
    if result.returncode:
        raise IdentityError("OpenSSL operation failed")
    return result.stdout


def private_directory(directory):
    directory = Path(directory)
    if directory.is_symlink():
        raise IdentityError("State directory is a symlink")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = directory.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise IdentityError("State directory ownership is invalid")
    directory.chmod(0o700)
    return directory


def ensure_identity(directory, *, profile="legacy"):
    if profile not in ("legacy", "apk"):
        raise IdentityError("Unknown app identity profile")
    directory = private_directory(directory)
    key = directory / "app_key.pem"
    cert = directory / "app_cert.pem"
    present = [p.exists() for p in (key, cert)]
    if any(present) and not all(present):
        raise IdentityError("Incomplete identity; refusing to overwrite")
    if all(present):
        for path in (key, cert):
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise IdentityError("Invalid identity file")
            path.chmod(0o600)
        return key, cert
    with tempfile.TemporaryDirectory(dir=directory) as temporary:
        temporary = Path(temporary)
        new_key = temporary / "key.pem"
        new_cert = temporary / "cert.pem"
        _openssl(
            [
                "ecparam",
                "-name",
                "brainpoolP320r1",
                "-param_enc",
                "explicit",
                "-genkey",
                "-noout",
                "-out",
                str(new_key),
            ]
        )
        new_key.chmod(0o600)
        if profile == "apk":
            # APK ecgroup.generate_certificate: v3, serial 1, self-issued,
            # C=DK/O=Trifork/CN=localhost; SKI, AKI, critical non-CA.
            # Native validity is 2,000,000,000 seconds; CLI rounds to days.
            arguments = [
                "-days",
                "23148",
                "-set_serial",
                "1",
                "-subj",
                "/C=DK/O=Trifork/CN=localhost",
                "-addext",
                "subjectKeyIdentifier=hash",
                "-addext",
                "authorityKeyIdentifier=keyid:always",
                "-addext",
                "basicConstraints=critical,CA:FALSE",
            ]
        else:
            arguments = [
                "-days",
                "3650",
                "-subj",
                "/CN=Home Assistant Danfoss Link test",
                "-addext",
                "subjectKeyIdentifier=hash",
            ]
        _openssl(
            [
                "req",
                "-new",
                "-x509",
                "-key",
                str(new_key),
                "-sha256",
                *arguments,
                "-out",
                str(new_cert),
            ]
        )
        new_cert.chmod(0o600)
        # Hard links prevent overwriting an existing identity in concurrent setup.
        os.link(new_key, key)
        os.link(new_cert, cert)
    return key, cert
