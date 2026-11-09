"""Юнит-тесты предметной логики: без файлов, процессов и сети.

    python3 -m unittest discover -s tests -p "test_*.py"
"""
import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from tips.domain.errors import ERRORS, PaymentError  # noqa: E402
from tips.domain.money import calc_amounts, fmt, to_money  # noqa: E402
from tips.domain.qr_sign import check_exp, compute_signature, parse_time, verify_signature  # noqa: E402
from tips.domain.reports import report_metrics  # noqa: E402
from tips.domain.validation import parse_day, validate_qr_request, validate_request  # noqa: E402

SECRET = b"unit-test-secret"
QR = {"merchant_id": "m_001", "employee_id": "e_001", "receipt_id": "r_1001",
      "nonce": "c82561", "exp": "2025-11-01T23:59:00Z"}


class MoneyTests(unittest.TestCase):
    def test_half_up_rounding_not_float(self):
        self.assertEqual(calc_amounts(Decimal("53.50"), 5), (Decimal("2.68"), Decimal("56.18")))

    def test_spec_examples(self):
        self.assertEqual(calc_amounts(Decimal("637.00"), 5), (Decimal("31.85"), Decimal("668.85")))
        self.assertEqual(calc_amounts(Decimal("911.00"), 15), (Decimal("136.65"), Decimal("1047.65")))

    def test_own_tip_amount_is_kept(self):
        self.assertEqual(calc_amounts(Decimal("100.00"), tip_amount=Decimal("12.34")), (Decimal("12.34"), Decimal("112.34")))

    def test_to_money_rejects_extra_digits_and_garbage(self):
        for bad in ("1185.004", "abc", "NaN", "Infinity"):
            with self.assertRaises(PaymentError) as ctx:
                to_money(bad)
            self.assertEqual(ctx.exception.code, "VALIDATION_ERROR")

    def test_to_money_accepts_two_digits(self):
        self.assertEqual(to_money(Decimal("637.0")), Decimal("637.00"))
        self.assertEqual(fmt(to_money("5")), "5.00")


class SignatureTests(unittest.TestCase):
    def setUp(self):
        self.qr = dict(QR, signature=compute_signature(SECRET, QR))

    def test_signature_is_16_hex(self):
        self.assertRegex(self.qr["signature"], r"^[0-9a-f]{16}$")

    def test_valid_signature(self):
        self.assertTrue(verify_signature(SECRET, self.qr, self.qr["signature"]))

    def test_link_signature_must_match_too(self):
        self.assertFalse(verify_signature(SECRET, self.qr, "0" * 16))
        self.assertFalse(verify_signature(SECRET, self.qr, None))

    def test_tampered_field_breaks_signature(self):
        self.assertFalse(verify_signature(SECRET, dict(self.qr, receipt_id="r_1002"), self.qr["signature"]))

    def test_other_secret_breaks_signature(self):
        self.assertFalse(verify_signature(b"other", self.qr, self.qr["signature"]))

    def test_exp_boundary_is_inclusive(self):
        exp = parse_time(QR["exp"])
        self.assertTrue(check_exp(QR, exp))
        self.assertFalse(check_exp(QR, exp.replace(second=1)))


class ValidationTests(unittest.TestCase):
    def good(self, **extra):
        body = {"qr_id": "qr_001", "receipt_id": "r_1001", "base_amount": 1, "tip_pct": 5}
        body.update(extra)
        return body

    def code(self, fn, *args):
        with self.assertRaises(PaymentError) as ctx:
            fn(*args)
        return ctx.exception.code

    def test_valid_request_returns_key(self):
        self.assertEqual(validate_request(self.good(idempotency_key="k")), "k")

    def test_missing_key_is_generated(self):
        self.assertTrue(validate_request(self.good()))

    def test_exactly_one_tip_field(self):
        self.assertEqual(self.code(validate_request, self.good(tip_amount=3)), "VALIDATION_ERROR")
        body = self.good()
        del body["tip_pct"]
        self.assertEqual(self.code(validate_request, body), "VALIDATION_ERROR")

    def test_bad_ids(self):
        self.assertEqual(self.code(validate_request, self.good(qr_id="x")), "VALIDATION_ERROR")
        self.assertEqual(self.code(validate_request, self.good(receipt_id="1001")), "VALIDATION_ERROR")

    def test_qr_request(self):
        ok = {"merchant_id": "m_001", "employee_id": "e_001", "receipt_id": "r_2001", "amount": "10.50"}
        self.assertEqual(validate_qr_request(ok), Decimal("10.50"))
        self.assertEqual(self.code(validate_qr_request, dict(ok, merchant_id="m1")), "VALIDATION_ERROR")

    def test_parse_day(self):
        self.assertEqual(parse_day("2025-11-01").isoformat(), "2025-11-01")
        self.assertEqual(self.code(parse_day, "2025-13-01"), "VALIDATION_ERROR")


class ReportTests(unittest.TestCase):
    def test_only_success_counts_for_money(self):
        rows = [
            {"status": "SUCCESS", "tip": "10.00", "amount": "100.00", "timestamp": "2025-11-01T10:00:00Z"},
            {"status": "SUCCESS", "tip": "20.00", "amount": "100.00", "timestamp": "2025-11-02T10:00:00Z"},
            {"status": "DECLINED", "tip": "99.00", "amount": "100.00", "timestamp": "2025-11-02T11:00:00Z"},
        ]
        m = report_metrics(rows)
        self.assertEqual(m["tips_total"], 30.0)
        self.assertEqual(m["payments_count"], 3)
        self.assertEqual(m["avg_tip_pct"], 15.0)
        self.assertEqual(m["success_rate"], 0.67)
        self.assertEqual(m["top_day"], "2025-11-02")

    def test_empty_period(self):
        m = report_metrics([])
        self.assertEqual((m["tips_total"], m["payments_count"], m["avg_tip_pct"], m["success_rate"], m["top_day"]),
                         (0.0, 0, None, 0.0, None))


class ErrorCatalogTests(unittest.TestCase):
    def test_every_error_has_http_retryable_and_text(self):
        for code, (http, retryable, message) in ERRORS.items():
            self.assertIn(http, (400, 403, 404, 409, 410, 422), code)
            self.assertIsInstance(retryable, bool, code)
            self.assertTrue(message, code)


if __name__ == "__main__":
    unittest.main()
