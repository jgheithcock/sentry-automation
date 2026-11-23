"""Run: python3 -m unittest discover tests"""
import unittest

from billing.amounts import invoice_total, parse_amount


class ParseAmountTests(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(parse_amount("12.50"), 12.5)

    def test_dollar_sign(self):
        self.assertEqual(parse_amount("$12.50"), 12.5)

    def test_whitespace(self):
        self.assertEqual(parse_amount("  7 "), 7.0)


class InvoiceTotalTests(unittest.TestCase):
    def test_sum(self):
        self.assertEqual(invoice_total(["10.00", "$2.50"]), 12.5)

    def test_tax(self):
        self.assertEqual(invoice_total(["100"], tax_rate=0.0825), 108.25)


if __name__ == "__main__":
    unittest.main()
