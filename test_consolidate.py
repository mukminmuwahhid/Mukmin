"""
Unit tests for consolidate.py.

Run with:  python -m pytest test_consolidate.py -v
(or just:  python -m unittest test_consolidate.py -v)
"""

import unittest
from datetime import date

from consolidate import (
    Reference,
    classify_category,
    deduplicate,
    normalise_email,
    normalise_name,
    normalise_phone,
    parse_date,
    validate_row,
)


def make_ref():
    return Reference(
        categories={"Food & Beverage", "Grocery & Convenience", "Health & Beauty"},
        region_pic={
            "Central": ("Aishah Rahman", "aishah.rahman@example-ops.test"),
            "Northern": ("Tan Wei Ming", "weiming.tan@example-ops.test"),
        },
        existing_ids={"M0001"},
        existing_names={"gerai harapan enterprise"},
    )


class TestNormalisation(unittest.TestCase):
    def test_normalise_name_collapses_whitespace_and_case(self):
        self.assertEqual(normalise_name("  Gerai   Emas  "), "gerai emas")

    def test_normalise_email_lowercases_and_trims(self):
        self.assertEqual(normalise_email("  Foo@BAR.com "), "foo@bar.com")

    def test_normalise_phone_plain_digits(self):
        self.assertEqual(normalise_phone("03-1176911"), "031176911")

    def test_normalise_phone_plus60_prefix(self):
        self.assertEqual(normalise_phone("+60123456789"), "0123456789")

    def test_normalise_phone_bare_60_prefix(self):
        self.assertEqual(normalise_phone("60123456789"), "0123456789")

    def test_normalise_phone_non_digits_stripped(self):
        self.assertEqual(normalise_phone("phone n/a"), "")

    def test_parse_date_iso(self):
        self.assertEqual(parse_date("2026-01-18"), date(2026, 1, 18))

    def test_parse_date_dmy(self):
        self.assertEqual(parse_date("21/3/2026"), date(2026, 3, 21))

    def test_parse_date_invalid(self):
        self.assertIsNone(parse_date("not-a-date"))


class TestClassifyCategory(unittest.TestCase):
    def test_known_keyword_maps_correctly(self):
        self.assertEqual(classify_category("nasi lemak stall"), "Food & Beverage")
        self.assertEqual(classify_category("mini grocer"), "Grocery & Convenience")
        self.assertEqual(classify_category("phone repair"), "Electronics & Repair")

    def test_unmappable_returns_none(self):
        self.assertIsNone(classify_category("n/a"))
        self.assertIsNone(classify_category("other"))
        self.assertIsNone(classify_category("general trading sdn bhd"))

    def test_blank_returns_none(self):
        self.assertIsNone(classify_category(""))


class TestValidateRow(unittest.TestCase):
    def base_row(self, **overrides):
        row = {
            "submission_id": "A001",
            "merchant_name": "Gerai Emas",
            "business_category_freetext": "kopitiam",
            "region": "Central",
            "contact_phone": "03-1234567",
            "contact_email": "test@example.com",
            "registration_date": "2026-01-01",
            "existing_merchant_id": "",
            "source_file": "submissions_partnerA.csv",
        }
        row.update(overrides)
        return row

    def test_valid_row_passes(self):
        result = validate_row(self.base_row(), make_ref())
        self.assertTrue(result.ok)
        self.assertEqual(result.normalised["canonical_category"], "Food & Beverage")

    def test_missing_required_field(self):
        result = validate_row(self.base_row(merchant_name=""), make_ref())
        self.assertFalse(result.ok)
        self.assertTrue(any("Missing required field" in r for r in result.reasons))

    def test_invalid_region(self):
        result = validate_row(self.base_row(region="Atlantis"), make_ref())
        self.assertFalse(result.ok)
        self.assertTrue(any("Invalid region" in r for r in result.reasons))

    def test_invalid_email(self):
        result = validate_row(self.base_row(contact_email="not-an-email"), make_ref())
        self.assertFalse(result.ok)
        self.assertTrue(any("Invalid email" in r for r in result.reasons))

    def test_short_phone(self):
        result = validate_row(self.base_row(contact_phone="12345"), make_ref())
        self.assertFalse(result.ok)
        self.assertTrue(any("Invalid phone" in r for r in result.reasons))

    def test_unmappable_category(self):
        result = validate_row(
            self.base_row(business_category_freetext="n/a"), make_ref()
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("Unmappable business category" in r for r in result.reasons))

    def test_future_date_rejected(self):
        result = validate_row(
            self.base_row(registration_date="2026-12-25"), make_ref()
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("future" in r for r in result.reasons))

    def test_already_onboarded_by_id(self):
        result = validate_row(
            self.base_row(existing_merchant_id="M0001"), make_ref()
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("already onboarded" in r for r in result.reasons))

    def test_already_onboarded_by_name(self):
        result = validate_row(
            self.base_row(merchant_name="  gerai HARAPAN  enterprise "), make_ref()
        )
        self.assertFalse(result.ok)
        self.assertTrue(any("already onboarded" in r for r in result.reasons))

    def test_multiple_simultaneous_failures_all_reported(self):
        result = validate_row(
            self.base_row(region="Atlantis", contact_email="bad", contact_phone="1"),
            make_ref(),
        )
        self.assertFalse(result.ok)
        self.assertEqual(len(result.reasons), 3)


class TestDeduplicate(unittest.TestCase):
    def test_collapses_by_normalised_name_keeps_most_recent(self):
        records = [
            {
                "_dedup_key": "gerai emas", "merchant_name": "Gerai Emas",
                "registration_date": "2026-01-01", "source_submission_id": "A001",
                "canonical_category": "Food & Beverage", "region": "Central",
                "contact_phone": "0311111111", "contact_email": "a@x.com",
                "region_pic_email": "pic@x.com",
            },
            {
                "_dedup_key": "gerai emas", "merchant_name": "GERAI EMAS",
                "registration_date": "2026-03-01", "source_submission_id": "B010",
                "canonical_category": "Food & Beverage", "region": "Central",
                "contact_phone": "0322222222", "contact_email": "b@x.com",
                "region_pic_email": "pic@x.com",
            },
        ]
        result = deduplicate(records)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["source_submission_id"], "B010")
        self.assertEqual(result[0]["duplicates_collapsed"], 2)

    def test_no_duplicates_passthrough(self):
        records = [
            {
                "_dedup_key": "a", "merchant_name": "A", "registration_date": "2026-01-01",
                "source_submission_id": "A001", "canonical_category": "x", "region": "y",
                "contact_phone": "1", "contact_email": "a@x.com", "region_pic_email": "p",
            },
            {
                "_dedup_key": "b", "merchant_name": "B", "registration_date": "2026-01-01",
                "source_submission_id": "A002", "canonical_category": "x", "region": "y",
                "contact_phone": "1", "contact_email": "b@x.com", "region_pic_email": "p",
            },
        ]
        result = deduplicate(records)
        self.assertEqual(len(result), 2)


if __name__ == "__main__":
    unittest.main()
