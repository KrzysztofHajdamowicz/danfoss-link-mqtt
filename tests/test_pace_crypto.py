import unittest
from danfoss_mdg.pace_crypto import PaceCrypto
from danfoss_mdg.pairing import PairingError


class NativePaceTests(unittest.TestCase):
    def setUp(self):
        self.crypto = PaceCrypto()

    def tearDown(self):
        self.crypto.close()

    def test_aes256_zero_key_known_vector(self):
        encrypted = self.crypto.aes(bytes(16), bytes(16), bytes(16), encrypt=True)
        self.assertEqual(encrypted.hex(), "dc95c078a2408989ad48a21492842087")
        self.assertEqual(self.crypto.aes(encrypted, bytes(16), bytes(16)), bytes(16))

    def test_ec_mapping_shared_secret_agrees(self):
        a = (7).to_bytes(40, "big")
        b = (11).to_bytes(40, "big")
        pi = (13).to_bytes(40, "big")
        x = self.crypto.multiply(a)
        y = self.crypto.multiply(b)
        mapped_a = self.crypto.add(self.crypto.multiply(pi), self.crypto.multiply(a, y))
        mapped_b = self.crypto.add(self.crypto.multiply(pi), self.crypto.multiply(b, x))
        self.assertEqual(mapped_a, mapped_b)
        pk_a = self.crypto.multiply(a, mapped_a)
        pk_b = self.crypto.multiply(b, mapped_b)
        self.assertEqual(self.crypto.multiply(a, pk_b), self.crypto.multiply(b, pk_a))

    def test_invalid_points_and_scalar_rejected(self):
        one = (1).to_bytes(40, "big")
        for point in (b"\x00", bytes(81), b"\x04" + bytes(80), b"\x04" + b"\xff" * 80):
            with self.assertRaises(PairingError):
                self.crypto.multiply(one, point)
        for scalar in (bytes(40), b"\xff" * 40, bytes(39)):
            with self.assertRaises(PairingError):
                self.crypto.multiply(scalar)

    def test_scalar_envelope_preserves_first_40_bytes(self):
        scalar = (19).to_bytes(40, "big")
        plain = scalar + bytes(range(24))
        encrypted = self.crypto.aes(plain, b"k" * 16, bytes(16), encrypt=True)
        self.assertEqual(
            self.crypto.decode_scalar(encrypted, b"k" * 16, b"i" * 16), scalar
        )
        self.assertNotEqual(
            self.crypto.aes(encrypted, b"k" * 16, b"i" * 16)[:40], scalar
        )
        with self.assertRaises(PairingError):
            self.crypto.decode_scalar(encrypted, b"k" * 16, b"bad")
        with self.assertRaises(PairingError):
            self.crypto.decode_scalar(encrypted[:-1], b"k" * 16, b"i" * 16)

    def test_nonce_mapping_accepts_full_width_and_zero(self):
        order = int(
            "D35E472036BC4FB7E13C785ED201E065F98FCFA5B68F12A32D482EC7EE8658E98691555B44C59311",
            16,
        )
        one = (1).to_bytes(40, "big")
        shared = self.crypto.multiply((7).to_bytes(40, "big"))
        expected = self.crypto.add(self.crypto.multiply(one), shared)
        self.assertEqual(
            self.crypto.mapped_generator((order + 1).to_bytes(40, "big"), shared),
            expected,
        )
        self.assertEqual(self.crypto.mapped_generator(bytes(40), shared), shared)
        self.assertEqual(
            self.crypto.mapped_generator(order.to_bytes(40, "big"), shared), shared
        )
        with self.assertRaises(PairingError):
            self.crypto.multiply((order + 1).to_bytes(40, "big"))

    def test_random_scalars_support_valid_public_points(self):
        a = self.crypto.random_scalar()
        b = self.crypto.random_scalar()
        self.assertNotEqual(a, b)
        self.assertEqual(len(self.crypto.multiply(a)), 81)
        self.assertEqual(len(self.crypto.multiply(b)), 81)


if __name__ == "__main__":
    unittest.main()
