import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from hh_live_eligibility import assess, canonical_hh_url, salary_floor


class HHLiveEligibilityTests(unittest.TestCase):
    def row(self, **changes):
        values = {
            "title": "AI Product Manager",
            "location": "remote",
            "metadata": json.dumps({"salary": "от 180 000 ₽"}),
        }
        values.update(changes)
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute("create table x(title,location,metadata)")
        con.execute("insert into x values(?,?,?)", tuple(values.values()))
        return con.execute("select * from x").fetchone()

    def test_canonical_url_strips_tracking(self):
        self.assertEqual(canonical_hh_url("https://hh.ru/vacancy/123?x=1", "123"), "https://hh.ru/vacancy/123")

    def test_salary_floor(self):
        self.assertEqual(salary_floor({"salary": "от 150 000 до 220 000 ₽"}), 150000)
        self.assertIsNone(salary_floor({}))

    def test_active_remote_target_is_eligible(self):
        raw = '<div data-qa="vacancy-description">Remote product discovery and operations</div>'
        ok, reasons = assess({"external_id": "123", "job_title": "AI Product Manager"}, self.row(), raw, "https://hh.ru/vacancy/123")
        self.assertTrue(ok, reasons)

    def test_archived_is_rejected(self):
        raw = '<script>{"archived": "true"}</script><div data-qa="vacancy-description">remote</div>'
        ok, reasons = assess({"external_id": "123", "job_title": "AI Product Manager"}, self.row(), raw, "https://hh.ru/vacancy/123")
        self.assertFalse(ok)
        self.assertIn("vacancy_archived", reasons)

    def test_low_salary_and_excluded_title_are_rejected(self):
        raw = '<div data-qa="vacancy-description">remote</div>'
        ok, reasons = assess({"external_id": "123", "job_title": "Product Support Manager"}, self.row(metadata=json.dumps({"salary": "100 000 ₽"})), raw, "https://hh.ru/vacancy/123")
        self.assertFalse(ok)
        self.assertIn("excluded_title_family", reasons)
        self.assertIn("salary_floor_below_130k", reasons)


if __name__ == "__main__":
    unittest.main()
