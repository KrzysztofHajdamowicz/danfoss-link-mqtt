"""Accept explicit EC encoding only for the exact known BrainpoolP320r1 group.

This compatibility policy accepts OpenSSL error 94 only after an independent
parameter and public-key check. Every other certificate error remains fatal.
Trust anchors, signatures, validity and chain validation stay with OpenSSL.
"""

import ctypes as C
import ctypes.util

EXPLICIT_EC_PARAMETERS = 94


class CurveMatcher:
    def __init__(self, curve_name="brainpoolP320r1"):
        if curve_name not in (
            "brainpoolP320r1",
            "brainpoolP256r1",
            "brainpoolP384r1",
            "brainpoolP512r1",
            "prime256v1",
            "secp384r1",
            "secp521r1",
        ):
            raise ValueError("Unsupported diagnostic curve")
        name = ctypes.util.find_library("crypto")
        if not name:
            raise RuntimeError("System libcrypto unavailable")
        self.lib = C.CDLL(name)
        signatures = {
            "d2i_X509": (C.c_void_p, [C.c_void_p, C.POINTER(C.c_void_p), C.c_long]),
            "X509_get_pubkey": (C.c_void_p, [C.c_void_p]),
            "EVP_PKEY_get1_EC_KEY": (C.c_void_p, [C.c_void_p]),
            "EC_KEY_get0_group": (C.c_void_p, [C.c_void_p]),
            "EC_KEY_check_key": (C.c_int, [C.c_void_p]),
            "OBJ_txt2nid": (C.c_int, [C.c_char_p]),
            "EC_GROUP_new_by_curve_name": (C.c_void_p, [C.c_int]),
            "EC_GROUP_cmp": (C.c_int, [C.c_void_p, C.c_void_p, C.c_void_p]),
            "EC_GROUP_get0_cofactor": (C.c_void_p, [C.c_void_p]),
            "BN_cmp": (C.c_int, [C.c_void_p, C.c_void_p]),
        }
        for function, (result, arguments) in signatures.items():
            fn = getattr(self.lib, function)
            fn.restype = result
            fn.argtypes = arguments
        for function in ("X509_free", "EVP_PKEY_free", "EC_KEY_free", "EC_GROUP_free"):
            fn = getattr(self.lib, function)
            fn.restype = None
            fn.argtypes = [C.c_void_p]
        self.nid = self.lib.OBJ_txt2nid(curve_name.encode("ascii"))
        if self.nid <= 0:
            raise RuntimeError("Required curve unavailable")

    def matches(self, der):
        if not isinstance(der, bytes) or not 1 <= len(der) <= 65535:
            return False
        lib = self.lib
        certificate = key = ec_key = expected = None
        buffer = C.create_string_buffer(der)
        pointer = C.c_void_p(C.addressof(buffer))
        try:
            certificate = lib.d2i_X509(None, C.byref(pointer), len(der))
            if not certificate or pointer.value != C.addressof(buffer) + len(der):
                return False
            key = lib.X509_get_pubkey(certificate)
            if not key:
                return False
            ec_key = lib.EVP_PKEY_get1_EC_KEY(key)
            if not ec_key:
                return False
            group = lib.EC_KEY_get0_group(ec_key)
            expected = lib.EC_GROUP_new_by_curve_name(self.nid)
            if not group or not expected:
                return False
            if lib.EC_GROUP_cmp(group, expected, None) != 0:
                return False
            actual_cofactor = lib.EC_GROUP_get0_cofactor(group)
            expected_cofactor = lib.EC_GROUP_get0_cofactor(expected)
            if not actual_cofactor or not expected_cofactor:
                return False
            if lib.BN_cmp(actual_cofactor, expected_cofactor) != 0:
                return False
            return lib.EC_KEY_check_key(ec_key) == 1
        finally:
            if expected:
                lib.EC_GROUP_free(expected)
            if ec_key:
                lib.EC_KEY_free(ec_key)
            if key:
                lib.EVP_PKEY_free(key)
            if certificate:
                lib.X509_free(certificate)


class CertificatePolicy:
    def __init__(self):
        self.matcher = CurveMatcher()
        self.compatibility_checks = 0
        self.last_error = None
        self.rejected_curve = None

    def verify(self, connection, certificate, error, depth, valid):
        if valid:
            return True
        self.last_error = error
        if error != EXPLICIT_EC_PARAMETERS:
            return False
        try:
            from OpenSSL import crypto

            accepted = self.matcher.matches(
                crypto.dump_certificate(crypto.FILETYPE_ASN1, certificate)
            )
        except Exception:
            return False
        if accepted:
            self.compatibility_checks += 1
        else:
            self.rejected_curve = "unrecognized"
            der = crypto.dump_certificate(crypto.FILETYPE_ASN1, certificate)
            for name in (
                "brainpoolP256r1",
                "brainpoolP384r1",
                "brainpoolP512r1",
                "prime256v1",
                "secp384r1",
                "secp521r1",
            ):
                if CurveMatcher(name).matches(der):
                    self.rejected_curve = name
                    break
        return accepted
