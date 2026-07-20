#!/usr/bin/env python3
import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).with_name("hh_browser_collect_to_sheets.py")
spec = importlib.util.spec_from_file_location("hh_collector", MODULE_PATH)
assert spec is not None and spec.loader is not None
hh = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = hh
spec.loader.exec_module(hh)


class SearchFiltersTests(unittest.TestCase):
    def test_reads_only_enabled_hh_rows_and_expands_or_queries(self):
        rows = [
            ["enabled", "platform", "track", "query", "location/area", "remote_only", "salary_min", "currency"],
            ["yes", "hh.ru", "Product", "Product Manager OR Product Owner", "remote", "yes", "150000", "RUB"],
            ["no", "hh.ru", "Disabled", "Project Manager", "remote", "yes", "", ""],
            ["yes", "LinkedIn", "Other", "AI Product Manager", "Worldwide", "yes", "", "USD"],
        ]

        filters = hh.parse_search_filter_rows(rows)

        self.assertEqual([f.query for f in filters], ["Product Manager", "Product Owner"])
        self.assertTrue(all(f.remote_only for f in filters))
        self.assertTrue(all(f.salary_min == 150000 for f in filters))
        self.assertTrue(all(f.label == "Product" for f in filters))


class TitleQualityTests(unittest.TestCase):
    def test_rejects_obvious_off_target_titles(self):
        for title in [
            "Продакт-менеджер | Product manager (fashion)",
            "Product Marketing Manager",
            "Product Analyst",
            "Product Designer",
            "Customer Support Product Manager",
        ]:
            with self.subTest(title=title):
                self.assertFalse(hh.is_target_title(title))

    def test_accepts_target_product_project_and_operator_titles(self):
        for title in [
            "AI Product Manager",
            "Руководитель продукта",
            "Digital Project Manager",
            "Founder Associate",
            "Chief of Staff",
        ]:
            with self.subTest(title=title):
                self.assertTrue(hh.is_target_title(title))


class SheetMergeTests(unittest.TestCase):
    def test_updates_duplicate_and_preserves_human_state_without_duplicating(self):
        old = hh.Vacancy(
            status="applied",
            job_title="Old title",
            job_url="https://hh.ru/vacancy/123?from=old",
            next_action="wait",
            notes="human note",
        )
        new = hh.Vacancy(
            status="scored",
            job_title="Updated title",
            job_url="https://hh.ru/vacancy/123?from=new",
            next_action="review",
            notes="collector note",
        )
        existing = [hh.COLUMNS, [getattr(old, c) for c in hh.COLUMNS]]

        values, stats = hh.merge_sheet_values(existing, [new])

        self.assertEqual(len(values), 2)
        row = dict(zip(values[0], values[1]))
        self.assertEqual(row["job_title"], "Updated title")
        self.assertEqual(row["status"], "applied")
        self.assertEqual(row["next_action"], "wait")
        self.assertIn("human note", row["notes"])
        self.assertEqual(stats, {"added": 0, "updated": 1, "unchanged": 0, "total": 1})

    def test_collapses_preexisting_duplicate_urls(self):
        first = hh.Vacancy(job_title="First", job_url="https://hh.ru/vacancy/777?x=1")
        second = hh.Vacancy(job_title="Second", job_url="https://hh.ru/vacancy/777?x=2")
        existing = [
            hh.COLUMNS,
            [getattr(first, c) for c in hh.COLUMNS],
            [getattr(second, c) for c in hh.COLUMNS],
        ]

        values, stats = hh.merge_sheet_values(existing, [])

        self.assertEqual(len(values), 2)
        self.assertEqual(stats["total"], 1)


class CollectionResilienceTests(unittest.TestCase):
    def test_continues_with_next_filter_when_search_request_fails(self):
        filters = [hh.SearchFilter("first", "first"), hh.SearchFilter("second", "second")]
        vacancy = hh.Vacancy(job_title="Product Manager", job_url="https://hh.ru/vacancy/9", fit_score="80")
        with patch.object(hh, "parse_search", side_effect=[OSError("network"), [("Product Manager", vacancy.job_url)]]), \
             patch.object(hh, "parse_detail", return_value=vacancy), \
             patch.object(hh.time, "sleep"):
            rows = hh.collect(filters, per_query=1, max_total=1)

        self.assertEqual([row.job_url for row in rows], [vacancy.job_url])


if __name__ == "__main__":
    unittest.main()
