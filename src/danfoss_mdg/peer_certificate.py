"""Inspect self-signed or grid-issued PACE certificates before OTP authentication.

A valid self-signature is not identity authentication. Only a successful PACE
proof bound to this exact public PEM authorizes contact exchange or persistence.
"""

import ctypes as C
import hmac
from pathlib import Path
from .certificate_policy import CurveMatcher
from .pairing import PairingError
from .diagnostics import progress


def candidate_public_key(
    der, ca_file=None, chain=(), *, allow_missing_issuer=False, trusted_der=None
):
    if trusted_der is not None and (
        not isinstance(trusted_der, bytes) or not hmac.compare_digest(der, trusted_der)
    ):
        raise PairingError("Peer certificate differs from saved PACE trust")
    matcher = CurveMatcher()
    if not matcher.matches(der):
        raise PairingError("Peer certificate uses an unexpected curve")
    lib = matcher.lib
    p = C.c_void_p
    signatures = {
        "X509_get_subject_name": (p, [p]),
        "X509_get_issuer_name": (p, [p]),
        "X509_NAME_cmp": (C.c_int, [p, p]),
        "X509_verify": (C.c_int, [p, p]),
        "X509_get0_notBefore": (p, [p]),
        "X509_get0_notAfter": (p, [p]),
        "X509_cmp_current_time": (C.c_int, [p]),
        "BIO_s_mem": (p, []),
        "BIO_new": (p, [p]),
        "BIO_free": (C.c_int, [p]),
        "BIO_ctrl": (C.c_long, [p, C.c_int, C.c_long, p]),
        "PEM_write_bio_PUBKEY": (C.c_int, [p, p]),
        "X509_STORE_new": (p, []),
        "X509_STORE_free": (None, [p]),
        "X509_STORE_load_locations": (C.c_int, [p, C.c_char_p, C.c_char_p]),
        "X509_STORE_CTX_new": (p, []),
        "X509_STORE_CTX_free": (None, [p]),
        "X509_STORE_CTX_init": (C.c_int, [p, p, p, p]),
        "X509_verify_cert": (C.c_int, [p]),
        "X509_STORE_CTX_get_error": (C.c_int, [p]),
        "OPENSSL_sk_new_null": (p, []),
        "OPENSSL_sk_push": (C.c_int, [p, p]),
        "OPENSSL_sk_free": (None, [p]),
        "ERR_clear_error": (None, []),
    }
    for name, (result, args) in signatures.items():
        fn = getattr(lib, name)
        fn.restype = result
        fn.argtypes = args
    buf = C.create_string_buffer(der)
    ptr = p(C.addressof(buf))
    cert = key = bio = store = ctx = untrusted = None
    intermediates = []
    try:
        cert = lib.d2i_X509(None, C.byref(ptr), len(der))
        if not cert or ptr.value != C.addressof(buf) + len(der):
            raise PairingError("Invalid peer certificate")
        if (
            lib.X509_cmp_current_time(lib.X509_get0_notBefore(cert)) != -1
            or lib.X509_cmp_current_time(lib.X509_get0_notAfter(cert)) != 1
        ):
            raise PairingError("Peer certificate validity failed")
        key = lib.X509_get_pubkey(cert)
        if not key:
            raise PairingError("Peer certificate public key absent")
        self_signed = lib.X509_verify(cert, key) == 1
        if not self_signed:
            # Issuer-signed certificates are expected, including pinned CCs.
            # This exploratory self-signature check must not poison TLS reads.
            lib.ERR_clear_error()
        if not self_signed and trusted_der is None:
            lib.ERR_clear_error()
            if (
                lib.X509_NAME_cmp(
                    lib.X509_get_subject_name(cert), lib.X509_get_issuer_name(cert)
                )
                == 0
            ):
                raise PairingError("Peer certificate self-signature failed")
            # Actual CC can present a grid-issued certificate, rather than
            # the app-style self-signed certificate used in synthetic tests.
            ca_file = (
                Path(ca_file)
                if ca_file
                else Path(__file__).with_name("trusted-CAs.pem")
            )
            if len(chain) > 8:
                raise PairingError("Peer certificate chain too long")
            untrusted = lib.OPENSSL_sk_new_null()
            if not untrusted:
                raise PairingError("Peer certificate chain allocation failed")
            for intermediate_der in chain:
                if intermediate_der == der:
                    continue
                if (
                    not isinstance(intermediate_der, bytes)
                    or not 1 <= len(intermediate_der) <= 65535
                ):
                    raise PairingError("Invalid peer intermediate certificate")
                buffer = C.create_string_buffer(intermediate_der)
                pointer = p(C.addressof(buffer))
                intermediate = lib.d2i_X509(
                    None, C.byref(pointer), len(intermediate_der)
                )
                if not intermediate:
                    raise PairingError("Invalid peer intermediate certificate")
                intermediates.append(intermediate)
                if (
                    pointer.value != C.addressof(buffer) + len(intermediate_der)
                    or lib.OPENSSL_sk_push(untrusted, intermediate) <= 0
                ):
                    raise PairingError("Invalid peer intermediate certificate")
            store = lib.X509_STORE_new()
            ctx = lib.X509_STORE_CTX_new()
            if (
                not store
                or not ctx
                or lib.X509_STORE_load_locations(store, str(ca_file).encode(), None)
                != 1
                or lib.X509_STORE_CTX_init(ctx, store, cert, untrusted) != 1
            ):
                raise PairingError("Peer certificate trust setup failed")
            if lib.X509_verify_cert(ctx) != 1:
                error = lib.X509_STORE_CTX_get_error(ctx)
                # Explicit pairing-only bootstrap, never a trusted identity.
                # Only issuer-not-found is eligible. Validity, exact curve,
                # native TLS key-possession proof and mandatory PACE remain.
                if not (allow_missing_issuer and error == 20):
                    raise PairingError(
                        f"Peer certificate chain verification failed (OpenSSL code {error}); "
                        f"TLS chain certificates={len(chain)}; self-signature valid=no"
                    )
                # Native certificate diagnostics must not leave stale errors
                # on the thread queue used by subsequent TLS reads/writes.
                lib.ERR_clear_error()
                progress("peer_issuer_missing")
        bio = lib.BIO_new(lib.BIO_s_mem())
        if not bio or lib.PEM_write_bio_PUBKEY(bio, key) != 1:
            raise PairingError("Peer public key export failed")
        data = p()
        length = lib.BIO_ctrl(bio, 3, 0, C.byref(data))
        if not 1 <= length <= 8192:
            raise PairingError("Invalid peer public key size")
        return C.string_at(data, length)
    finally:
        if ctx:
            lib.X509_STORE_CTX_free(ctx)
        if store:
            lib.X509_STORE_free(store)
        if untrusted:
            lib.OPENSSL_sk_free(untrusted)
        for intermediate in intermediates:
            lib.X509_free(intermediate)
        if bio:
            lib.BIO_free(bio)
        if key:
            lib.EVP_PKEY_free(key)
        if cert:
            lib.X509_free(cert)
