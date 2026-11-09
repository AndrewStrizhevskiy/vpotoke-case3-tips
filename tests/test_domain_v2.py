"""Предметная логика API v2 без ввода-вывода: идентификаторы, деньги, QR версии 2.

    python3 -m unittest tests.test_domain_v2
"""
import base64
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from tips.domain import ids  # noqa: E402
from tips.domain.errors import PaymentError  # noqa: E402
from tips.domain.money import parse_money_v2  # noqa: E402
from tips.domain.qr_sign import compute_signature  # noqa: E402
from tips.domain.qr_v2 import QrV2Keys, qr_version, sign_v2, verify_v2  # noqa: E402

KEY = bytes(range(32))
QR = {"qr_id": "qr_0123456789abcdefghjkmnpqrs", "merchant_id": "m_001", "employee_id": "e_001", "receipt_id": "r_1001",
      "nonce": "AAAAAAAAAAAAAAAAAAAAAA", "exp": "2025-11-01T23:59:00Z"}
VECTOR = "hz3KFMitmdOV7Zucjjb9Ug"  # зафиксированный эталон: любое изменение схемы подписи его ломает


class IdTests(unittest.TestCase):
    def test_format_and_prefix(self):
        for prefix, pattern in (("qr", ids.QR_V2), ("pay", ids.PAYMENT_V2), ("rf", ids.REFUND_V2)):
            value = ids.new_id(prefix)
            self.assertRegex(value, pattern.pattern)
            self.assertEqual(len(value), len(prefix) + 1 + 26)

    def test_random_and_unique(self):
        seen = {ids.new_id("pay") for _ in range(5000)}
        self.assertEqual(len(seen), 5000)
        alphabet = set("".join(v[4:] for v in seen))
        self.assertTrue(alphabet <= set(ids.ALPHABET))
        self.assertGreater(len(alphabet), 25)  # символы действительно разные, а не счётчик

    def test_not_sequential_and_no_time_prefix(self):
        a, b = ids.new_id("qr"), ids.new_id("qr")
        self.assertNotEqual(a[:8], b[:8])  # у ULID и UUIDv7 начало одинаковое: там время

    def test_patterns_do_not_overlap_with_v1(self):
        v1 = (re.compile(r"^qr_[0-9]{3,12}$"), re.compile(r"^p_[0-9]{4,12}$"))
        for old in ("qr_001", "qr_123456789012", "p_0101"):
            self.assertFalse(ids.QR_V2.match(old) or ids.PAYMENT_V2.match(old), old)
        for new in (ids.new_id("qr"), ids.new_id("pay")):
            self.assertFalse(any(p.match(new) for p in v1), new)

    def test_confusable_letters_are_rejected(self):
        for bad in ("qr_" + "i" * 26, "qr_" + "l" * 26, "qr_" + "o" * 26, "qr_" + "u" * 26, "qr_" + "A" * 26, "qr_" + "a" * 25,
                    "qr_" + "a" * 27, "pay_" + "a" * 26 + "\n"):
            self.assertIsNone(ids.QR_V2.match(bad) or ids.PAYMENT_V2.match(bad), bad)

    def test_idempotency_key_must_be_uuid_v4_in_canonical_form(self):
        good = "3f1c2a9e-7b4d-4e1a-9c2f-5d8e6b7a1c00"
        self.assertTrue(ids.IDEMPOTENCY_KEY_V2.match(good))
        for bad in (good.upper(), "{" + good + "}", "urn:uuid:" + good, good + "\n", good.replace("4e1a", "1e1a"),
                    good.replace("9c2f", "cc2f"), good + "0", "", "not-a-uuid"):
            self.assertIsNone(ids.IDEMPOTENCY_KEY_V2.match(bad), bad)


class MoneyV2Tests(unittest.TestCase):
    def test_accepts_exact_decimal_strings(self):
        for text, value in (("0.00", "0.00"), ("637.00", "637.00"), ("1000000.00", "1000000.00"), ("0.01", "0.01")):
            self.assertEqual(str(parse_money_v2(text)), value)

    def test_rejects_everything_else(self):
        for bad in (637, 637.0, 637.005, True, None, "637", "637.0", "637.000", "-1.00", "+1.00", "01.00", "1e3", " 1.00",
                    "1.00 ", "1,00", "١٢٣.٤٥", "NaN", "Infinity", "", "10000000.00", "1.00\n", [], {}):
            with self.assertRaises(PaymentError, msg=repr(bad)) as ctx:
                parse_money_v2(bad)
            self.assertEqual(ctx.exception.code, "VALIDATION_ERROR")


class QrV2Tests(unittest.TestCase):
    def setUp(self):
        self.keys = QrV2Keys({"k1": KEY}, "k1")
        self.row = dict(QR, version=2, key_id="k1", signature=sign_v2(self.keys, QR, "637.00"))

    def test_pinned_vector(self):
        self.assertEqual(self.row["signature"], VECTOR)
        self.assertEqual(len(base64.urlsafe_b64decode(VECTOR + "==")), 16)  # 128 бит

    def test_valid_signature(self):
        self.assertTrue(verify_v2(self.keys, self.row, "637.00", "RUB", VECTOR))

    def test_every_signed_field_matters(self):
        for field, value in (("qr_id", "qr_x123456789abcdefghjkmnpqrs"), ("merchant_id", "m_002"), ("employee_id", "e_002"),
                             ("receipt_id", "r_1002"), ("nonce", "BBBBBBBBBBBBBBBBBBBBBB"), ("exp", "2025-11-02T23:59:00Z"),
                             ("key_id", "k2")):
            row = dict(self.row, **{field: value})
            keys = QrV2Keys({"k1": KEY, "k2": b"x" * 32}, "k1")
            self.assertFalse(verify_v2(keys, row, "637.00", "RUB", VECTOR), field)
        self.assertFalse(verify_v2(self.keys, self.row, "637.01", "RUB", VECTOR))  # сумма чека изменена
        self.assertFalse(verify_v2(self.keys, self.row, "637.00", "USD", VECTOR))

    def test_link_signature_must_match_and_have_the_right_shape(self):
        for bad in ("A" * 22, "", None, 5, VECTOR[:-1], VECTOR + "A", VECTOR + "\n", "я" * 22, VECTOR.replace("h", "="),
                    "hz3KFMitmdOV7Zucjjb9U="):
            self.assertFalse(verify_v2(self.keys, self.row, "637.00", "RUB", bad), repr(bad))

    def test_unknown_or_missing_key_id_is_refused(self):
        self.assertFalse(verify_v2(QrV2Keys({"k2": KEY}, "k2"), self.row, "637.00", "RUB", VECTOR))
        for kid in (None, "", 7, ["k1"]):
            self.assertFalse(verify_v2(self.keys, dict(self.row, key_id=kid), "637.00", "RUB", VECTOR), repr(kid))

    def test_other_key_with_same_kid_fails(self):
        self.assertFalse(verify_v2(QrV2Keys({"k1": b"z" * 32}, "k1"), self.row, "637.00", "RUB", VECTOR))

    def test_rotation_new_active_key_old_key_still_verifies(self):
        rotated = QrV2Keys({"k1": KEY, "k2": b"n" * 32}, "k2")
        self.assertTrue(verify_v2(rotated, self.row, "637.00", "RUB", VECTOR))  # QR, выпущенный старым ключом
        fresh = dict(QR, qr_id="qr_" + "b" * 26, version=2, key_id="k2")
        fresh["signature"] = sign_v2(rotated, fresh, "100.00")
        self.assertTrue(verify_v2(rotated, fresh, "100.00", "RUB", fresh["signature"]))
        self.assertFalse(verify_v2(QrV2Keys({"k1": KEY}, "k1"), fresh, "100.00", "RUB", fresh["signature"]))

    def test_length_prefixes_stop_field_shifting(self):
        a = dict(self.row, merchant_id="m_00", employee_id="1e_001")
        b = dict(self.row, merchant_id="m_001", employee_id="e_001")
        self.assertNotEqual(sign_v2(self.keys, a, "637.00"), sign_v2(self.keys, b, "637.00"))

    def test_no_cross_version_signatures(self):
        v1_sig = compute_signature(b"demo-secret-k3", dict(QR, signature="x"))
        self.assertFalse(verify_v2(self.keys, dict(self.row, signature=v1_sig), "637.00", "RUB", v1_sig))
        self.assertEqual(len(v1_sig), 16)  # 16 hex-символов v1 не подходят под формат v2 (22 символа base64url)

    def test_key_ring_validation(self):
        rings = (({"k1": b"short"}, "k1"), ({"K1": KEY}, "K1"), ({"k1": KEY}, "k2"), ({"toolongkid1": KEY}, "toolongkid1"))
        for ring, active in rings:
            with self.assertRaises(ValueError, msg=str(ring)):
                QrV2Keys(ring, active)

    def test_version_helper(self):
        self.assertEqual(qr_version({"version": 2}), 2)
        self.assertEqual(qr_version({"version": "2"}), 2)
        self.assertEqual(qr_version({}), 1)  # строки CSV без столбца версии
        self.assertEqual(qr_version({"version": "x"}), 0)


if __name__ == "__main__":
    unittest.main()
