import importlib.util
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

SCRIPTS = Path(__file__).parent


def load(name: str):
    path = SCRIPTS / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class CollectionOutcomeTests(unittest.TestCase):
    def test_zero_rows_after_source_errors_is_error_not_success(self):
        reliability = load("operational_reliability")

        self.assertEqual(reliability.collection_status(0, [{"error": "network"}]), "error")

    def test_rows_with_source_errors_are_degraded_and_clean_run_is_success(self):
        reliability = load("operational_reliability")

        self.assertEqual(reliability.collection_status(2, [{"error": "one source"}]), "degraded")
        self.assertEqual(reliability.collection_status(0, []), "success")

    def test_linkedin_collector_returns_error_status_for_empty_errored_run(self):
        collector = load("collect_linkedin_public_to_sheets")
        fake_paths = (Path("rows.json"), Path("rows.csv"), Path("queue.json"))
        output = io.StringIO()
        with patch.object(sys, "argv", [collector.__file__, "https://example.test/jobs"]), \
             patch.object(collector, "collect", return_value=([], [{"stage": "source", "error": "boom"}])), \
             patch.object(collector, "write_outputs", return_value=fake_paths), \
             redirect_stdout(output):
            exit_code = collector.main()

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "error")

    def test_linkedin_canonicalizer_rejects_malformed_absolute_url(self):
        collector = load("collect_linkedin_public_to_sheets")
        self.assertEqual(collector.canon("https:///;"), "")
        self.assertEqual(collector.canon("javascript:alert(1)"), "")
        self.assertEqual(
            collector.canon("https://www.linkedin.com/jobs/view/product-manager-4439083216?trk=feed"),
            "https://www.linkedin.com/jobs/view/4439083216",
        )

    def test_job_board_collector_returns_error_status_for_empty_errored_run(self):
        collector = load("collect_job_boards_to_sheets")
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(sys, "argv", [collector.__file__, "--sources", "unknown"]), \
             patch.object(collector, "DATA_DIR", Path(tmp)), \
             redirect_stdout(output), redirect_stderr(io.StringIO()):
            exit_code = collector.main()

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "error")


class WrapperReliabilityTests(unittest.TestCase):
    def test_wrapper_forwards_child_stderr_and_propagates_degraded_status(self):
        wrapper = load("run_linkedin_public_sources")
        child = subprocess.CompletedProcess(
            ["collector"], 0,
            stdout=json.dumps({"status": "degraded", "rows": 3, "errors": [{"error": "partial"}]}),
            stderr="source warning on stderr\n",
        )
        captured_stderr = io.StringIO()
        with patch.object(wrapper.subprocess, "run", return_value=child), redirect_stderr(captured_stderr):
            result = wrapper.run_collector(["collector"])

        self.assertEqual(result["status"], "degraded")
        self.assertEqual(captured_stderr.getvalue(), "source warning on stderr\n")

    def test_wrapper_returns_structured_error_when_no_linkedin_urls_are_enabled(self):
        wrapper = load("run_linkedin_public_sources")
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "sources.json"
            config.write_text(json.dumps({"sources": [
                {"enabled": False, "type": "search_url", "url": "https://example.test/disabled"},
                {"enabled": True, "type": "other", "url": "https://example.test/not-search"},
            ]}))
            with patch.object(sys, "argv", [wrapper.__file__, "--config", str(config)]), redirect_stdout(output):
                exit_code = wrapper.main()

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["urls"], 0)

    def test_wrapper_discards_enabled_source_with_missing_url(self):
        wrapper = load("run_linkedin_public_sources")
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "sources.json"
            config.write_text(json.dumps({
                "sources": [{"enabled": True, "type": "search_url", "name": "missing"}],
            }))
            with patch.object(sys, "argv", [wrapper.__file__, "--config", str(config)]), \
                 redirect_stdout(output):
                exit_code = wrapper.main()

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload, {
            "status": "error", "urls": 0,
            "error": "no enabled LinkedIn search URLs",
        })

    def test_wrapper_discards_blank_and_whitespace_urls(self):
        wrapper = load("run_linkedin_public_sources")
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "sources.json"
            config.write_text(json.dumps({"sources": [
                {"enabled": True, "type": "search_url", "url": ""},
                {"enabled": True, "type": "search_url", "url": "   \t"},
            ]}))
            with patch.object(sys, "argv", [wrapper.__file__, "--config", str(config)]), \
                 redirect_stdout(output):
                exit_code = wrapper.main()

        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["urls"], 0)


class DuplicateGuardTests(unittest.TestCase):
    def test_submitted_application_blocks_tracking_variant_and_external_id_variant(self):
        reliability = load("operational_reliability")
        guard = reliability.DuplicateGuard.from_sources(applications=[{
            "status": "applied",
            "send_status": "sent",
            "job_url": "https://www.linkedin.com/jobs/view/ai-pm-4439083216/?utm_source=mail",
        }])

        self.assertTrue(guard.is_duplicate("https://linkedin.com/jobs/view/4439083216?trk=feed"))
        self.assertTrue(guard.is_duplicate("", external_id="linkedin:4439083216"))
        self.assertFalse(guard.is_duplicate("https://linkedin.com/jobs/view/9999999999"))

    def test_confirmed_json_receipt_blocks_same_vacancy(self):
        reliability = load("operational_reliability")
        with tempfile.TemporaryDirectory() as tmp:
            receipt = Path(tmp) / "result.json"
            receipt.write_text(json.dumps({
                "job": "https://hh.ru/vacancy/12345?from=feed",
                "read_back_verified": True,
                "stop_reason": "submitted",
            }))
            guard = reliability.DuplicateGuard.from_sources(receipt_dir=Path(tmp))

        self.assertTrue(guard.is_duplicate("https://www.hh.ru/vacancy/12345"))

    def test_submitted_sqlite_receipt_blocks_external_vacancy_id(self):
        reliability = load("operational_reliability")
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.sqlite3"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE application_receipts (job_url TEXT, external_vacancy_id TEXT, status TEXT, submitted INTEGER)")
            con.execute("INSERT INTO application_receipts VALUES (?, ?, ?, ?)", ("", "greenhouse:987", "submitted", 1))
            con.commit()
            con.close()
            guard = reliability.DuplicateGuard.from_sources(sqlite_path=db)

        self.assertTrue(guard.is_duplicate("", external_id="greenhouse:987"))
        self.assertFalse(guard.is_duplicate("", external_id="greenhouse:988"))

    def test_pre_send_queue_excludes_vacancy_already_submitted_under_url_variant(self):
        queue = load("build_approved_send_queue")
        headers = ["application_id", "status", "priority", "lane", "channel", "company", "job_title", "source", "job_url", "draft_text", "send_status"]
        apps = [
            headers,
            ["old", "applied", "1", "direct", "linkedin", "Acme", "PM", "linkedin", "https://linkedin.com/jobs/view/role-123/?utm_source=x", "", "sent"],
            ["dup", "approved_by_user_needs_final_send_approval", "1", "direct", "linkedin", "Acme", "PM", "linkedin", "https://www.linkedin.com/jobs/view/123?trk=feed", "draft", "not_sent"],
            ["fresh", "approved_by_user_needs_final_send_approval", "1", "direct", "linkedin", "NewCo", "PM", "linkedin", "https://linkedin.com/jobs/view/456", "draft", "not_sent"],
        ]
        app_dicts = [dict(zip(headers, row)) for row in apps[1:]]
        reliability = load("operational_reliability")
        guard = reliability.DuplicateGuard.from_sources(applications=app_dicts)

        rows, duplicates = queue.build_queue_rows(apps, guard)

        self.assertEqual([row[10] for row in rows], ["fresh"])
        self.assertEqual(duplicates, 1)


class AdversarialReliabilityTests(unittest.TestCase):
    def test_source_specific_url_canonicalization_and_host_boundaries(self):
        r = load("operational_reliability")
        self.assertNotIn("id:linkedin:123", r.vacancy_keys("https://evil.test/jobs/view/123"))
        self.assertNotIn("id:hh:123", r.vacancy_keys("https://evilhh.ru/vacancy/123"))
        self.assertIn("id:hh:123", r.vacancy_keys("https://spb.hh.ru/vacancy/123"))
        first = r.vacancy_keys("https://jobs.example/role?source=111#/job/111")
        second = r.vacancy_keys("https://jobs.example/role?source=222#/job/222")
        self.assertTrue(first.isdisjoint(second))
        self.assertIn("url:https://jobs.example/role?source=111#/job/111", first)

    def test_malformed_urls_blank_ids_and_source_qualified_ids(self):
        r = load("operational_reliability")
        for value in ("", " ", "https://", "://broken", "mailto:user@example.com"):
            with self.subTest(value=value):
                self.assertEqual(r.vacancy_keys(value, "  "), set())
        self.assertIn("id:linkedin:123", r.vacancy_keys("linkedin.com/jobs/view/123"))
        self.assertEqual(r.vacancy_keys("", "123", source="linkedin"), {"id:linkedin:123"})
        self.assertEqual(r.vacancy_keys("", "123", source="hh"), {"id:hh:123"})
        self.assertEqual(r.vacancy_keys("", "123"), set())

    def test_strict_source_extractors_and_hh_response_url(self):
        r = load("operational_reliability")
        self.assertNotIn("id:linkedin:123", r.vacancy_keys("https://linkedin.com/jobs/view/123-other"))
        keys = r.vacancy_keys("https://hh.ru/applicant/vacancy_response?vacancyId=134187468&hhtmFrom=vacancy")
        self.assertIn("id:hh:134187468", keys)

    def test_common_ats_variants_get_host_bound_source_ids(self):
        r = load("operational_reliability")
        self.assertIn("id:greenhouse:987", r.vacancy_keys("https://boards.greenhouse.io/acme/jobs/987"))
        self.assertIn("id:greenhouse:987", r.vacancy_keys("https://job-boards.greenhouse.io/acme/jobs/987?gh_src=x"))
        self.assertNotIn("id:greenhouse:987", r.vacancy_keys("https://evilgreenhouse.io/acme/jobs/987"))
        self.assertIn("id:lever:acme/role-1", r.vacancy_keys("https://jobs.lever.co/acme/role-1"))
        self.assertIn("id:workable:acme/role-2", r.vacancy_keys("https://apply.workable.com/acme/j/role-2"))
        self.assertIn("id:ashby:acme/role-3", r.vacancy_keys("https://jobs.ashbyhq.com/acme/role-3"))

    def test_outreach_sent_and_generic_done_do_not_mean_applied(self):
        r = load("operational_reliability")
        guard = r.DuplicateGuard.from_sources(applications=[
            {"status": "approved", "send_status": "sent", "source": "linkedin", "job_url": "https://linkedin.com/jobs/view/111"},
            {"status": "done", "source": "hh", "job_url": "https://hh.ru/vacancy/222"},
        ])
        self.assertFalse(guard.is_duplicate("https://linkedin.com/jobs/view/111"))
        self.assertFalse(guard.is_duplicate("https://hh.ru/vacancy/222"))

    def test_downstream_application_states_remain_duplicate_evidence(self):
        r = load("operational_reliability")
        statuses = (
            "interview", "interviewing", "screening", "test_task", "offer",
            "rejected", "withdrawn", "accepted", "hired",
        )
        guard = r.DuplicateGuard.from_sources(applications=[
            {"status": status, "source": "linkedin", "job_url": f"https://linkedin.com/jobs/view/{300 + index}"}
            for index, status in enumerate(statuses)
        ])
        for index, status in enumerate(statuses):
            with self.subTest(status=status):
                self.assertTrue(guard.is_duplicate(f"https://linkedin.com/jobs/view/{300 + index}"))

    def test_existing_hh_and_nested_readback_receipts_are_supported(self):
        r = load("operational_reliability")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "hh.json").write_text(json.dumps({"results": [
                {"id": "134202102", "applied": True, "url": "https://hh.ru/vacancy/134202102"},
                {"source": "linkedin", "id": "555", "read_back_verified": True,
                 "read_back_marker": "submission_confirmation"},
            ]}))
            guard = r.DuplicateGuard.from_sources(receipt_dirs=[root])
        self.assertTrue(guard.is_duplicate("https://hh.ru/applicant/vacancy_response?vacancyId=134202102"))
        self.assertTrue(guard.is_duplicate("", external_id="555", source="linkedin"))

    def test_explicit_outreach_receipt_is_not_application_receipt(self):
        r = load("operational_reliability")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "outreach.json").write_text(json.dumps({
                "action": "recruiter_outreach", "sent": True, "read_back_verified": True,
                "url": "https://linkedin.com/jobs/view/777",
            }))
            guard = r.DuplicateGuard.from_sources(receipt_dir=root)
        self.assertFalse(guard.is_duplicate("https://linkedin.com/jobs/view/777"))

    def test_spaced_linkedin_message_discriminator_is_outreach(self):
        r = load("operational_reliability")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "linkedin-message.json").write_text(json.dumps({
                "channel": "LinkedIn Message",
                "read_back_verified": True,
                "url": "https://linkedin.com/jobs/view/778",
            }))
            guard = r.DuplicateGuard.from_sources(receipt_dir=root)
        self.assertFalse(guard.is_duplicate("https://linkedin.com/jobs/view/778"))

    def test_hyphenated_recruiter_message_discriminator_is_outreach(self):
        r = load("operational_reliability")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "recruiter-message.json").write_text(json.dumps({
                "action_type": "recruiter-message",
                "read_back_verified": True,
                "url": "https://linkedin.com/jobs/view/779",
            }))
            guard = r.DuplicateGuard.from_sources(receipt_dir=root)
        self.assertFalse(guard.is_duplicate("https://linkedin.com/jobs/view/779"))

    def test_nested_outreach_discriminator_blocks_envelope_readback_evidence(self):
        r = load("operational_reliability")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "nested-outreach.json").write_text(json.dumps({
                "read_back_verified": True,
                "url": "https://linkedin.com/jobs/view/780",
                "receipt": {"kind": "outreach", "send_status": "sent"},
            }))
            guard = r.DuplicateGuard.from_sources(receipt_dir=root)
        self.assertFalse(guard.is_duplicate("https://linkedin.com/jobs/view/780"))

    def test_implicit_outreach_fields_do_not_turn_readback_into_application_evidence(self):
        r = load("operational_reliability")
        field_values = {
            "action": "message",
            "action_type": "recruiter_message",
            "kind": "outreach",
            "channel": "linkedin_message",
            "send_status": "outreach_sent",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index, (field, value) in enumerate(field_values.items()):
                (root / f"outreach-{index}.json").write_text(json.dumps({
                    field: value,
                    "send_status": "sent" if field != "send_status" else value,
                    "read_back_verified": True,
                    "url": f"https://linkedin.com/jobs/view/{780 + index}",
                }))
            guard = r.DuplicateGuard.from_sources(receipt_dir=root)
        for index in range(len(field_values)):
            self.assertFalse(guard.is_duplicate(f"https://linkedin.com/jobs/view/{780 + index}"))

    def test_malformed_receipt_store_fails_closed(self):
        r = load("operational_reliability")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "broken.json").write_text("{not-json")
            with self.assertRaises(r.EvidenceStoreError):
                r.DuplicateGuard.from_sources(receipt_dir=root)

    def test_sqlite_strict_booleans_and_explicit_tables(self):
        r = load("operational_reliability")
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state with spaces.sqlite3"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE application_receipts (job_url TEXT, external_vacancy_id TEXT, source TEXT, status TEXT, submitted TEXT, read_back_verified TEXT)")
                con.execute("INSERT INTO application_receipts VALUES (?, ?, ?, ?, ?, ?)", ("https://hh.ru/vacancy/101", "101", "hh", "", "0", "0"))
                con.execute("CREATE TABLE misleading (job_url TEXT, submitted INTEGER)")
                con.execute("INSERT INTO misleading VALUES (?, ?)", ("https://hh.ru/vacancy/202", 1))
            guard = r.DuplicateGuard.from_sources(sqlite_path=db)
        self.assertFalse(guard.is_duplicate("https://hh.ru/vacancy/101"))
        self.assertFalse(guard.is_duplicate("https://hh.ru/vacancy/202"))

    def test_explicit_sqlite_without_supported_receipt_table_fails_closed(self):
        r = load("operational_reliability")
        with tempfile.TemporaryDirectory() as tmp:
            for filename, create_sql in (
                ("unrelated.sqlite3", "CREATE TABLE unrelated (id INTEGER)"),
                ("empty.sqlite3", ""),
            ):
                db = Path(tmp) / filename
                with sqlite3.connect(db) as con:
                    if create_sql:
                        con.execute(create_sql)
                with self.subTest(filename=filename), self.assertRaisesRegex(
                    r.EvidenceStoreError, "no supported receipt table"
                ):
                    r.DuplicateGuard.from_sources(sqlite_path=db)

    def test_sqlite_read_bound_fails_closed_instead_of_truncating(self):
        r = load("operational_reliability")
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "receipts.sqlite3"
            with sqlite3.connect(db) as con:
                con.execute("CREATE TABLE application_receipts (job_url TEXT, submitted INTEGER)")
                con.executemany("INSERT INTO application_receipts VALUES (?, 1)", [
                    ("https://hh.ru/vacancy/1",), ("https://hh.ru/vacancy/2",),
                ])
            with patch.object(r, "SQLITE_ROW_LIMIT", 1), self.assertRaises(r.EvidenceStoreError):
                r.DuplicateGuard.from_sources(sqlite_path=db)

    def test_corrupt_or_missing_sqlite_store_fails_closed(self):
        r = load("operational_reliability")
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "broken.sqlite3"
            db.write_bytes(b"not sqlite")
            with self.assertRaises(r.EvidenceStoreError):
                r.DuplicateGuard.from_sources(sqlite_path=db)
            with self.assertRaises(r.EvidenceStoreError):
                r.DuplicateGuard.from_sources(sqlite_path=Path(tmp) / "missing.sqlite3")

    def test_queue_rejects_identityless_and_deduplicates_same_run(self):
        queue = load("build_approved_send_queue")
        r = load("operational_reliability")
        headers = ["application_id", "status", "priority", "lane", "channel", "company", "job_title", "source", "job_url", "external_vacancy_id", "draft_text", "send_status"]
        eligible = "approved_by_user_needs_final_send_approval"
        apps = [headers,
            ["blank", eligible, "1", "direct", "linkedin", "A", "PM", "linkedin", " ", " ", "draft", "not_sent"],
            ["one", eligible, "1", "direct", "linkedin", "A", "PM", "linkedin", "https://linkedin.com/jobs/view/900", "", "draft", "not_sent"],
            ["two", eligible, "1", "direct", "linkedin", "A", "PM", "linkedin", "https://www.linkedin.com/jobs/view/900?trk=feed", "", "draft", "not_sent"],
        ]
        rows, duplicates = queue.build_queue_rows(apps, r.DuplicateGuard(frozenset()))
        self.assertEqual([row[10] for row in rows], ["one"])
        self.assertEqual(duplicates, 1)
        malformed_rows, _ = queue.build_queue_rows([["application_id", "status"], ["", eligible]], r.DuplicateGuard(frozenset()))
        self.assertEqual(malformed_rows, [])

    def test_queue_fails_before_any_sheet_write_when_evidence_is_untrusted(self):
        queue = load("build_approved_send_queue")
        service = MagicMock()
        service.spreadsheets().values().get().execute.return_value = {"values": []}
        with patch.object(queue, "svc", return_value=service), \
             patch.object(queue, "ensure_sheet") as ensure_sheet, \
             patch.object(queue.DuplicateGuard, "from_sources", side_effect=RuntimeError("bad evidence")), \
             self.assertRaises(RuntimeError):
            queue.main()
        ensure_sheet.assert_not_called()
        service.spreadsheets().values().clear.assert_not_called()

    def test_wrapper_parse_exit_consistency_and_degraded_code(self):
        wrapper = load("run_linkedin_public_sources")
        cases = [
            (subprocess.CompletedProcess(["c"], 0, stdout="", stderr=""), "error"),
            (subprocess.CompletedProcess(["c"], 0, stdout="[]", stderr=""), "error"),
            (subprocess.CompletedProcess(["c"], 7, stdout=json.dumps({"status": "success"}), stderr=""), "error"),
        ]
        for child, expected in cases:
            with self.subTest(child=child), patch.object(wrapper.subprocess, "run", return_value=child):
                self.assertEqual(wrapper.run_collector(["c"])["status"], expected)
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "sources.json"
            config.write_text(json.dumps({"sources": [{"enabled": True, "type": "search_url", "url": "https://example.test"}]}))
            with patch.object(sys, "argv", [wrapper.__file__, "--config", str(config), "--dry-run"]), \
                 patch.object(wrapper, "run_collector", return_value={"status": "degraded", "exit_code": 1, "stdout": "{}", "stderr": ""}), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(wrapper.main(), 1)

    def test_collectors_return_one_for_degraded(self):
        linkedin = load("collect_linkedin_public_to_sheets")
        fake_paths = (Path("rows.json"), Path("rows.csv"), Path("queue.json"))
        row = type("Row", (), {"enrichment_status": "partial"})()
        with patch.object(sys, "argv", [linkedin.__file__, "https://example.test/jobs"]), \
             patch.object(linkedin, "collect", return_value=([row], [{"error": "partial"}])), \
             patch.object(linkedin, "write_outputs", return_value=fake_paths), redirect_stdout(io.StringIO()):
            self.assertEqual(linkedin.main(), 1)
        boards = load("collect_job_boards_to_sheets")
        board_row = type("Job", (), {"job_url": "https://example.test/1"})()
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(sys, "argv", [boards.__file__, "--sources", "remotive,unknown"]), \
             patch.object(boards, "DATA_DIR", Path(tmp)), patch.object(boards, "remotive", return_value=[board_row]), \
             patch.object(boards, "write_artifacts", return_value=(Path("a.json"), Path("a.csv"))), \
             patch.object(boards.time, "sleep"), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            self.assertEqual(boards.main(), 1)


if __name__ == "__main__":
    unittest.main()
