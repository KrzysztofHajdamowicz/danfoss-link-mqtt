"""BrainpoolP320r1 and AES operations delegated to native libcrypto."""

import ctypes as C
import ctypes.util
from contextlib import contextmanager
from .pairing import PairingError


class PaceCrypto:
    def __init__(self):
        name = ctypes.util.find_library("crypto")
        if not name:
            raise PairingError("Native cryptography unavailable")
        self.lib = C.CDLL(name)
        p = C.c_void_p
        signatures = {
            "OBJ_txt2nid": (C.c_int, [C.c_char_p]),
            "EC_GROUP_new_by_curve_name": (p, [C.c_int]),
            "EC_GROUP_free": (None, [p]),
            "EC_GROUP_get0_order": (p, [p]),
            "EC_POINT_new": (p, [p]),
            "EC_POINT_clear_free": (None, [p]),
            "EC_POINT_oct2point": (C.c_int, [p, p, p, C.c_size_t, p]),
            "EC_POINT_point2oct": (C.c_size_t, [p, p, C.c_int, p, C.c_size_t, p]),
            "EC_POINT_is_on_curve": (C.c_int, [p, p, p]),
            "EC_POINT_is_at_infinity": (C.c_int, [p, p]),
            "EC_POINT_mul": (C.c_int, [p, p, p, p, p, p]),
            "EC_POINT_add": (C.c_int, [p, p, p, p, p]),
            "BN_new": (p, []),
            "BN_CTX_new": (p, []),
            "BN_CTX_free": (None, [p]),
            "BN_nnmod": (C.c_int, [p, p, p, p]),
            "BN_bin2bn": (p, [p, C.c_int, p]),
            "BN_bn2binpad": (C.c_int, [p, p, C.c_int]),
            "BN_priv_rand_range": (C.c_int, [p, p]),
            "BN_cmp": (C.c_int, [p, p]),
            "BN_num_bits": (C.c_int, [p]),
            "BN_set_flags": (None, [p, C.c_int]),
            "BN_clear_free": (None, [p]),
            "EVP_CIPHER_CTX_new": (p, []),
            "EVP_CIPHER_CTX_free": (None, [p]),
            "EVP_aes_256_cbc": (p, []),
            "EVP_CipherInit_ex": (C.c_int, [p, p, p, p, p, C.c_int]),
            "EVP_CIPHER_CTX_set_padding": (C.c_int, [p, C.c_int]),
            "EVP_CipherUpdate": (C.c_int, [p, p, C.POINTER(C.c_int), p, C.c_int]),
            "EVP_CipherFinal_ex": (C.c_int, [p, p, C.POINTER(C.c_int)]),
        }
        for name, (result, args) in signatures.items():
            fn = getattr(self.lib, name)
            fn.restype = result
            fn.argtypes = args
        self.group = self.lib.EC_GROUP_new_by_curve_name(
            self.lib.OBJ_txt2nid(b"brainpoolP320r1")
        )
        if not self.group:
            raise PairingError("Required PACE curve unavailable")

    def close(self):
        if self.group:
            self.lib.EC_GROUP_free(self.group)
            self.group = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @contextmanager
    def scalar(self, value=None):
        if not self.group:
            raise PairingError("PACE crypto context closed")
        if value is not None and (not isinstance(value, bytes) or len(value) != 40):
            raise PairingError("Invalid PACE scalar")
        bn = self.lib.BN_new()
        if not bn:
            raise PairingError("Native allocation failed")
        try:
            # Keep native secret scalar arithmetic on OpenSSL's constant-time path.
            self.lib.BN_set_flags(bn, 4)  # BN_FLG_CONSTTIME
            order = self.lib.EC_GROUP_get0_order(self.group)
            if value is None:
                for _ in range(8):
                    if self.lib.BN_priv_rand_range(bn, order) != 1:
                        raise PairingError("Native random scalar failed")
                    if self.lib.BN_num_bits(bn) > 0:
                        break
            elif not self.lib.BN_bin2bn(C.create_string_buffer(value), 40, bn):
                raise PairingError("Native scalar conversion failed")
            if self.lib.BN_num_bits(bn) == 0 or self.lib.BN_cmp(bn, order) >= 0:
                raise PairingError("PACE scalar outside group order")
            yield bn
        finally:
            self.lib.BN_clear_free(bn)

    def random_scalar(self):
        out = C.create_string_buffer(40)
        try:
            with self.scalar() as bn:
                if self.lib.BN_bn2binpad(bn, out, 40) != 40:
                    raise PairingError("Native scalar conversion failed")
            return out.raw
        finally:
            C.memset(out, 0, 40)

    @contextmanager
    def point(self, value=None):
        if not self.group:
            raise PairingError("PACE crypto context closed")
        if value is not None and (
            not isinstance(value, bytes) or len(value) != 81 or value[0] != 4
        ):
            raise PairingError("Invalid PACE point encoding")
        point = self.lib.EC_POINT_new(self.group)
        if not point:
            raise PairingError("Native allocation failed")
        try:
            if value is not None:
                if (
                    self.lib.EC_POINT_oct2point(
                        self.group, point, C.create_string_buffer(value), 81, None
                    )
                    != 1
                    or self.lib.EC_POINT_is_on_curve(self.group, point, None) != 1
                    or self.lib.EC_POINT_is_at_infinity(self.group, point) != 0
                ):
                    raise PairingError("Invalid PACE curve point")
            yield point
        finally:
            self.lib.EC_POINT_clear_free(point)

    def serialize(self, point):
        if self.lib.EC_POINT_is_at_infinity(self.group, point) != 0:
            raise PairingError("PACE result is infinity")
        out = C.create_string_buffer(81)
        try:
            if self.lib.EC_POINT_point2oct(self.group, point, 4, out, 81, None) != 81:
                raise PairingError("Native point conversion failed")
            return out.raw
        finally:
            C.memset(out, 0, 81)

    def multiply(self, scalar, point=None):
        with self.scalar(scalar) as bn, self.point() as result:
            if point is None:
                ok = self.lib.EC_POINT_mul(self.group, result, bn, None, None, None)
            else:
                with self.point(point) as source:
                    ok = self.lib.EC_POINT_mul(
                        self.group, result, None, source, bn, None
                    )
            if ok != 1:
                raise PairingError("Native point multiplication failed")
            return self.serialize(result)

    def add(self, left, right):
        with self.point(left) as a, self.point(right) as b, self.point() as result:
            if self.lib.EC_POINT_add(self.group, result, a, b, None) != 1:
                raise PairingError("Native point addition failed")
            return self.serialize(result)

    def mapped_generator(self, nonce, shared_point):
        # APK L20 converts any 40-byte nonce to BN; L23 computes s*G+H.
        # This nonce is not a locally generated ephemeral scalar. Normalize
        # modulo the group order before native constant-time multiplication.
        if not isinstance(nonce, bytes) or len(nonce) != 40:
            raise PairingError("Invalid PACE nonce")
        bn = self.lib.BN_new()
        ctx = self.lib.BN_CTX_new()
        if not bn or not ctx:
            if bn:
                self.lib.BN_clear_free(bn)
            if ctx:
                self.lib.BN_CTX_free(ctx)
            raise PairingError("Native allocation failed")
        try:
            self.lib.BN_set_flags(bn, 4)
            if (
                not self.lib.BN_bin2bn(C.create_string_buffer(nonce), 40, bn)
                or self.lib.BN_nnmod(
                    bn, bn, self.lib.EC_GROUP_get0_order(self.group), ctx
                )
                != 1
            ):
                raise PairingError("Native nonce conversion failed")
            with self.point(shared_point) as shared, self.point() as mapped:
                if (
                    self.lib.EC_POINT_mul(self.group, mapped, bn, None, None, ctx) != 1
                    or self.lib.EC_POINT_add(self.group, mapped, mapped, shared, ctx)
                    != 1
                ):
                    raise PairingError("Native PACE mapping failed")
                return self.serialize(mapped)
        finally:
            self.lib.BN_clear_free(bn)
            self.lib.BN_CTX_free(ctx)

    def aes(self, data, key, iv, *, encrypt=False):
        if (
            not isinstance(data, bytes)
            or not 16 <= len(data) <= 65520
            or len(data) % 16
            or not isinstance(key, bytes)
            or len(key) != 16
            or not isinstance(iv, bytes)
            or len(iv) != 16
        ):
            raise PairingError("Invalid PACE AES inputs")
        # APK init at 0xaa4d4..0xaa558 zero-fills key buffer then copies key bytes.
        keybuf = C.create_string_buffer(key + bytes(16))
        ivbuf = C.create_string_buffer(iv)
        out = C.create_string_buffer(len(data) + 16)
        written = C.c_int()
        final = C.c_int()
        ctx = self.lib.EVP_CIPHER_CTX_new()
        if not ctx:
            raise PairingError("Native allocation failed")
        try:
            if (
                self.lib.EVP_CipherInit_ex(
                    ctx, self.lib.EVP_aes_256_cbc(), None, keybuf, ivbuf, int(encrypt)
                )
                != 1
                or self.lib.EVP_CIPHER_CTX_set_padding(ctx, 0) != 1
                or self.lib.EVP_CipherUpdate(
                    ctx, out, C.byref(written), C.create_string_buffer(data), len(data)
                )
                != 1
                or self.lib.EVP_CipherFinal_ex(
                    ctx, C.byref(out, written.value), C.byref(final)
                )
                != 1
            ):
                raise PairingError("Native PACE cipher failed")
            return out.raw[: written.value + final.value]
        finally:
            self.lib.EVP_CIPHER_CTX_free(ctx)
            C.memset(keybuf, 0, len(keybuf))
            C.memset(ivbuf, 0, len(ivbuf))
            C.memset(out, 0, len(out))

    def decode_scalar(self, encrypted, key, iv):
        if not isinstance(encrypted, bytes) or len(encrypted) != 64:
            raise PairingError("Invalid PACE scalar envelope")
        if not isinstance(iv, bytes) or len(iv) != 16:
            raise PairingError("Invalid PACE AES inputs")
        # Original pace.client_part converts hello.iv with lbn.text. The
        # native AES wrapper calls lua_tolstring on that BN userdata, obtains
        # NULL, and leaves its zero-filled IV buffer unchanged. Preserve this
        # app compatibility behavior only for the PACE nonce decode.
        return self.aes(encrypted, key, bytes(16))[:40]
