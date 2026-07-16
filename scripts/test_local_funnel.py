import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parent
CORE_SRC = Path("/opt/data/job-funnel-public/src")
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))


def load_module():
    path = SCRIPTS / "local_funnel.py"
    spec = importlib.util.spec_from_file_location("local_funnel", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["local_funnel"] = module
    spec.loader.exec_module(module)
    return module


def row(**overrides):
    value = {
        "source": "linkedin",
        "job_title": "AI Product Manager",
        "company": "Acme",
        "job_url": "https://www.linkedin.com/jobs/view/4439083216?trk=feed",
        "remote_location": "Remote",
        "fit_score": "92",
        "status": "scored",
        "next_action": "apply",
        "why_relevant": "AI product ownership",
        "hr_email": "recruiter@example.com",
    }
    value.update(overrides)
    return value


def test_import_is_local_idempotent_and_enqueues_high_fit_actions(tmp_path):
    mod = load_module()
    db = tmp_path / "funnel.sqlite3"
    with mod.LocalFunnel(db) as funnel:
        first = funnel.import_rows([
            row(),
            row(
                source="telegram",
                job_title="Product Lead",
                company="Agency",
                job_url="https://t.me/product_jobs/2090",
                fit_score="100",
                hr_email="",
            ),
            row(
                job_url="https://www.linkedin.com/jobs/view/111",
                fit_score="30",
                hr_email="",
            ),
        ])
        second = funnel.import_rows([row()])
        summary = funnel.summary()

    assert first == {"accepted": 3, "created": 3, "updated": 0, "duplicates": 0, "queued": 3, "rejected": 0}
    assert second == {"accepted": 1, "created": 0, "updated": 0, "duplicates": 1, "queued": 0, "rejected": 0}
    assert summary["jobs"] == 3
    assert summary["queue"] == {"pending": 3}

    con = sqlite3.connect(db)
    kinds = [r[0] for r in con.execute("SELECT kind FROM queue ORDER BY id")]
    assert kinds == ["application_review", "email_outreach_draft", "application_review"]
    payload_text = "\n".join(r[0] for r in con.execute("SELECT payload FROM queue"))
    assert "gmail_app_password" not in payload_text
    assert "recruiter@example.com" in payload_text


def test_import_rejects_identityless_or_unsafe_rows_without_partial_queue(tmp_path):
    mod = load_module()
    db = tmp_path / "funnel.sqlite3"
    bad = row(job_url="", hr_email="attacker@example.com")
    unsafe = row(job_url="https://user:pass@example.com/jobs/1")
    with mod.LocalFunnel(db) as funnel:
        result = funnel.import_rows([bad, unsafe])
        summary = funnel.summary()
    assert result["accepted"] == 0
    assert result["rejected"] == 2
    assert summary["jobs"] == 0
    assert summary["queue"] == {}


def test_hh_source_alias_uses_vacancy_id_instead_of_url_hash(tmp_path):
    mod = load_module()
    db = tmp_path / "funnel.sqlite3"
    with mod.LocalFunnel(db) as funnel:
        result = funnel.import_rows([row(
            source="hh.ru",
            job_url="https://spb.hh.ru/vacancy/134996267?from=search",
            hr_email="",
        )])
    assert result["accepted"] == 1
    con = sqlite3.connect(db)
    assert con.execute("SELECT source,external_id FROM jobs").fetchone() == ("hh", "134996267")


def test_existing_application_suppresses_review_queue_and_links_job(tmp_path):
    mod = load_module()
    db = tmp_path / "funnel.sqlite3"
    with mod.LocalFunnel(db) as funnel:
        funnel.record_application(
            source="linkedin", external_vacancy_id="4439083216",
            job_url="https://linkedin.com/jobs/view/4439083216",
            company="Acme", job_title="AI Product Manager", status="submitted",
            submitted_at="2026-07-12T10:00:00+00:00", read_back_verified=True,
            evidence_path="applications/acme/readback.png",
        )
        result = funnel.import_rows([row()])
        summary = funnel.summary()
    assert result["created"] == 1
    assert result["queued"] == 0
    assert summary["queue"] == {}
    con = sqlite3.connect(db)
    assert con.execute("SELECT job_id IS NOT NULL FROM application_receipts").fetchone()[0] == 1


def test_recording_application_completes_pending_initial_actions(tmp_path):
    mod = load_module()
    db = tmp_path / "funnel.sqlite3"
    with mod.LocalFunnel(db) as funnel:
        funnel.import_rows([row()])
        funnel.record_application(
            source="linkedin", external_vacancy_id="4439083216",
            job_url="https://linkedin.com/jobs/view/4439083216",
            company="Acme", job_title="AI Product Manager", status="submitted",
            submitted_at="2026-07-12T10:00:00+00:00", read_back_verified=True,
            evidence_path="applications/acme/readback.png",
        )
        summary = funnel.summary()
    assert summary["queue"] == {"done": 2}


def test_application_receipt_is_compatible_with_duplicate_guard(tmp_path):
    mod = load_module()
    db = tmp_path / "funnel.sqlite3"
    with mod.LocalFunnel(db) as funnel:
        funnel.import_rows([row()])
        receipt_id = funnel.record_application(
            source="linkedin",
            external_vacancy_id="4439083216",
            job_url="https://linkedin.com/jobs/view/4439083216",
            company="Acme",
            job_title="AI Product Manager",
            status="submitted",
            submitted_at="2026-07-12T10:00:00+00:00",
            read_back_verified=True,
            evidence_path="applications/acme/readback.png",
        )
        same = funnel.record_application(
            source="linkedin",
            external_vacancy_id="4439083216",
            job_url="https://www.linkedin.com/jobs/view/4439083216?trk=x",
            company="Acme",
            job_title="AI Product Manager",
            status="submitted",
            submitted_at="2026-07-12T10:00:00+00:00",
            read_back_verified=True,
            evidence_path="applications/acme/readback.png",
        )
    assert receipt_id == same

    reliability_path = SCRIPTS / "operational_reliability.py"
    spec = importlib.util.spec_from_file_location("operational_reliability_for_local", reliability_path)
    assert spec and spec.loader
    reliability = importlib.util.module_from_spec(spec)
    sys.modules["operational_reliability_for_local"] = reliability
    spec.loader.exec_module(reliability)
    guard = reliability.DuplicateGuard.from_sources(sqlite_path=db)
    assert guard.is_duplicate("https://www.linkedin.com/jobs/view/4439083216")


def test_followup_due_only_for_submitted_without_terminal_response(tmp_path):
    mod = load_module()
    db = tmp_path / "funnel.sqlite3"
    with mod.LocalFunnel(db) as funnel:
        funnel.record_application(
            source="hh", external_vacancy_id="100", job_url="https://hh.ru/vacancy/100",
            company="A", job_title="PM", status="submitted",
            submitted_at="2026-07-10T00:00:00+00:00", read_back_verified=True,
            evidence_path="a.png",
        )
        funnel.record_application(
            source="hh", external_vacancy_id="200", job_url="https://hh.ru/vacancy/200",
            company="B", job_title="PM", status="rejected",
            submitted_at="2026-07-10T00:00:00+00:00", read_back_verified=True,
            evidence_path="b.png", response_at="2026-07-14T00:00:00+00:00",
        )
        created = funnel.enqueue_due_followups(now="2026-07-16T00:00:00+00:00", after_days=5)
        again = funnel.enqueue_due_followups(now="2026-07-16T00:00:00+00:00", after_days=5)
    assert created == 1
    assert again == 0
    con = sqlite3.connect(db)
    assert con.execute("SELECT count(*) FROM queue WHERE kind='application_followup'").fetchone()[0] == 1


def test_action_intent_survives_restart_and_rejects_conflicting_reuse(tmp_path):
    mod = load_module()
    db = tmp_path / "funnel.sqlite3"
    payload = {
        "source": "linkedin",
        "external_id": "4439083216",
        "job_url": "https://www.linkedin.com/jobs/view/4439083216",
    }

    with mod.LocalFunnel(db) as funnel:
        run_id = funnel.begin_batch_run(
            channel="linkedin",
            max_actions=5,
            started_at="2026-07-16T10:00:00+00:00",
        )
        first = funnel.reserve_action_intent(
            run_id=run_id,
            kind="application_submit",
            idempotency_key="linkedin:4439083216:application",
            payload=payload,
            now="2026-07-16T10:00:01+00:00",
        )

    with mod.LocalFunnel(db) as reopened:
        replay = reopened.reserve_action_intent(
            run_id=run_id,
            kind="application_submit",
            idempotency_key="linkedin:4439083216:application",
            payload=payload,
            now="2026-07-16T10:05:00+00:00",
        )
        with pytest.raises(mod.ActionIntentConflict):
            reopened.reserve_action_intent(
                run_id=run_id,
                kind="application_submit",
                idempotency_key="linkedin:4439083216:application",
                payload={**payload, "external_id": "different"},
                now="2026-07-16T10:05:01+00:00",
            )

    assert first.intent_id == replay.intent_id
    assert first.created is True
    assert replay.created is False
    con = sqlite3.connect(db)
    assert con.execute("SELECT state FROM batch_runs WHERE id=?", (run_id,)).fetchone()[0] == "running"
    assert con.execute("SELECT count(*) FROM action_intents").fetchone()[0] == 1


def test_verified_receipt_closes_intent_attributes_run_and_enforces_quota(tmp_path):
    mod = load_module()
    db = tmp_path / "funnel.sqlite3"
    payload = {
        "source": "linkedin",
        "external_id": "4439083216",
        "job_url": "https://www.linkedin.com/jobs/view/4439083216",
    }
    with mod.LocalFunnel(db) as funnel:
        run_id = funnel.begin_batch_run(
            channel="linkedin", max_actions=1, started_at="2026-07-16T10:00:00+00:00"
        )
        reservation = funnel.reserve_action_intent(
            run_id=run_id,
            kind="application_submit",
            idempotency_key="linkedin:4439083216:application",
            payload=payload,
            now="2026-07-16T10:00:01+00:00",
        )
        receipt_id = funnel.record_application(
            source="linkedin",
            external_vacancy_id="4439083216",
            job_url=payload["job_url"],
            company="Acme",
            job_title="AI Product Manager",
            status="submitted",
            submitted_at="2026-07-16T10:02:00+00:00",
            read_back_verified=True,
            evidence_path="applications/acme/result.json",
            intent_id=reservation.intent_id,
        )
        with pytest.raises(mod.BatchQuotaExceeded):
            funnel.reserve_action_intent(
                run_id=run_id,
                kind="application_submit",
                idempotency_key="linkedin:999:application",
                payload={**payload, "external_id": "999"},
                now="2026-07-16T10:03:00+00:00",
            )

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    intent = con.execute("SELECT * FROM action_intents WHERE id=?", (reservation.intent_id,)).fetchone()
    receipt = con.execute("SELECT * FROM application_receipts WHERE id=?", (receipt_id,)).fetchone()
    assert intent["state"] == "verified"
    assert receipt["action_intent_id"] == reservation.intent_id
    assert receipt["run_id"] == run_id
    assert con.execute(
        "SELECT count(*) FROM action_intents WHERE run_id=? AND state='verified'", (run_id,)
    ).fetchone()[0] == 1


def test_ambiguous_side_effect_is_fenced_and_never_rereserved(tmp_path):
    mod = load_module()
    db = tmp_path / "funnel.sqlite3"
    payload = {
        "source": "linkedin",
        "external_id": "4439083216",
        "job_url": "https://www.linkedin.com/jobs/view/4439083216",
    }
    with mod.LocalFunnel(db) as funnel:
        run_id = funnel.begin_batch_run(
            channel="linkedin", max_actions=1, started_at="2026-07-16T10:00:00+00:00"
        )
        reservation = funnel.reserve_action_intent(
            run_id=run_id,
            kind="application_submit",
            idempotency_key="linkedin:4439083216:application",
            payload=payload,
            now="2026-07-16T10:00:01+00:00",
        )
        token = funnel.mark_intent_executing(
            intent_id=reservation.intent_id,
            worker_id="worker-a",
            now="2026-07-16T10:00:02+00:00",
        )
        with pytest.raises(mod.IntentFenceViolation):
            funnel.mark_intent_ambiguous(
                intent_id=reservation.intent_id,
                execution_token="stale-token",
                now="2026-07-16T10:00:03+00:00",
                error_code="transport_lost_after_submit",
            )
        funnel.mark_intent_ambiguous(
            intent_id=reservation.intent_id,
            execution_token=token,
            now="2026-07-16T10:00:04+00:00",
            error_code="transport_lost_after_submit",
        )

    with mod.LocalFunnel(db) as reopened:
        replay = reopened.reserve_action_intent(
            run_id=run_id,
            kind="application_submit",
            idempotency_key="linkedin:4439083216:application",
            payload=payload,
            now="2026-07-16T10:10:00+00:00",
        )
        with pytest.raises(mod.IntentFenceViolation):
            reopened.mark_intent_executing(
                intent_id=reservation.intent_id,
                worker_id="worker-b",
                now="2026-07-16T10:10:01+00:00",
            )

    assert replay.intent_id == reservation.intent_id
    assert replay.created is False
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    intent = con.execute("SELECT * FROM action_intents WHERE id=?", (reservation.intent_id,)).fetchone()
    assert intent["state"] == "ambiguous"
    assert intent["side_effect_maybe_at"] == "2026-07-16T10:00:04+00:00"
    assert intent["last_error_code"] == "transport_lost_after_submit"


def test_ambiguous_intent_is_reconciled_without_reexecuting_side_effect(tmp_path):
    mod = load_module()
    db = tmp_path / "funnel.sqlite3"
    payload = {
        "source": "linkedin",
        "external_id": "4439083999",
        "job_url": "https://www.linkedin.com/jobs/view/4439083999",
    }
    with mod.LocalFunnel(db) as funnel:
        run_id = funnel.begin_batch_run(
            channel="linkedin", max_actions=1, started_at="2026-07-16T10:00:00+00:00"
        )
        intent = funnel.reserve_action_intent(
            run_id=run_id,
            kind="application_submit",
            idempotency_key="linkedin:4439083999:application",
            payload=payload,
            now="2026-07-16T10:00:01+00:00",
        )
        execution_token = funnel.mark_intent_executing(
            intent_id=intent.intent_id, worker_id="submit-a", now="2026-07-16T10:00:02+00:00"
        )
        funnel.mark_intent_ambiguous(
            intent_id=intent.intent_id,
            execution_token=execution_token,
            now="2026-07-16T10:00:03+00:00",
            error_code="confirmation_read_failed",
        )

        claims = funnel.claim_due_reconciliations(
            worker_id="reconciler-a",
            limit=5,
            now="2026-07-16T10:00:04+00:00",
        )
        assert len(claims) == 1
        claim = claims[0]
        assert claim.intent_id == intent.intent_id
        assert claim.payload == payload
        assert claim.reconciliation_attempt == 1

        funnel.reschedule_reconciliation(
            intent_id=claim.intent_id,
            reconciliation_token=claim.reconciliation_token,
            next_reconcile_at="2026-07-16T10:15:00+00:00",
            now="2026-07-16T10:00:05+00:00",
            error_code="no_provider_evidence_yet",
        )
        assert funnel.claim_due_reconciliations(
            worker_id="reconciler-b", limit=5, now="2026-07-16T10:14:59+00:00"
        ) == []
        second = funnel.claim_due_reconciliations(
            worker_id="reconciler-b", limit=5, now="2026-07-16T10:15:00+00:00"
        )
        assert len(second) == 1
        assert second[0].reconciliation_attempt == 2
        with pytest.raises(mod.IntentFenceViolation):
            funnel.mark_intent_executing(
                intent_id=intent.intent_id,
                worker_id="submit-b",
                now="2026-07-16T10:15:01+00:00",
            )


def test_emergency_pause_and_batch_lifecycle_fail_closed(tmp_path):
    mod = load_module()
    db = tmp_path / "funnel.sqlite3"
    with mod.LocalFunnel(db) as funnel:
        run_id = funnel.begin_batch_run(
            channel="linkedin", max_actions=2, started_at="2026-07-16T10:00:00+00:00"
        )
        funnel.set_emergency_pause(
            paused=True,
            reason="operator emergency stop",
            now="2026-07-16T10:00:01+00:00",
        )
        with pytest.raises(mod.WorkflowPaused):
            funnel.begin_batch_run(
                channel="email", max_actions=1, started_at="2026-07-16T10:00:02+00:00"
            )
        with pytest.raises(mod.WorkflowPaused):
            funnel.reserve_action_intent(
                run_id=run_id,
                kind="application_submit",
                idempotency_key="linkedin:pause-test:application",
                payload={"source": "linkedin", "external_id": "pause-test"},
                now="2026-07-16T10:00:03+00:00",
            )
        health = funnel.workflow_health(now="2026-07-16T10:00:04+00:00")
        assert health["paused"] is True
        assert health["pause_reason"] == "operator emergency stop"
        assert health["running_batches"] == 1

        funnel.set_emergency_pause(
            paused=False,
            reason="transport verified",
            now="2026-07-16T10:00:05+00:00",
        )
        intent = funnel.reserve_action_intent(
            run_id=run_id,
            kind="application_submit",
            idempotency_key="linkedin:pause-test:application",
            payload={"source": "linkedin", "external_id": "pause-test"},
            now="2026-07-16T10:00:06+00:00",
        )
        assert intent.created is True
        funnel.heartbeat_batch_run(run_id=run_id, now="2026-07-16T10:00:07+00:00")
        funnel.finish_batch_run(
            run_id=run_id,
            state="completed",
            reason="bounded batch finished",
            now="2026-07-16T10:00:08+00:00",
        )
        with pytest.raises(ValueError, match="not running"):
            funnel.reserve_action_intent(
                run_id=run_id,
                kind="application_submit",
                idempotency_key="linkedin:after-finish:application",
                payload={"source": "linkedin", "external_id": "after-finish"},
                now="2026-07-16T10:00:09+00:00",
            )

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    run = con.execute("SELECT * FROM batch_runs WHERE id=?", (run_id,)).fetchone()
    assert run["state"] == "completed"
    assert run["heartbeat_at"] == "2026-07-16T10:00:07+00:00"
    assert run["finished_at"] == "2026-07-16T10:00:08+00:00"
    assert run["stop_reason"] == "bounded batch finished"


def test_email_intent_has_deterministic_message_id_and_verified_recipient_gate(tmp_path):
    mod = load_module()
    db = tmp_path / "funnel.sqlite3"
    kwargs = {
        "sender": "candidate@example.com",
        "recipient": "recruiter@example.org",
        "recipient_verified": True,
        "recipient_provenance": "company careers contact page",
        "vacancy_key": "example-org:ai-product-manager",
        "subject": "AI Product Manager — Alexander Shamshurin",
        "body": "Hello, I am reaching out about the AI Product Manager role. One relevant case is attached. Would a short conversation be useful?",
    }
    with mod.LocalFunnel(db) as funnel:
        run_id = funnel.begin_batch_run(
            channel="email", max_actions=5, started_at="2026-07-16T10:00:00+00:00"
        )
        first = funnel.prepare_email_intent(
            run_id=run_id, now="2026-07-16T10:00:01+00:00", **kwargs
        )
        assert first.created is True
        assert first.message_id.startswith("<job-search-")
        assert first.message_id.endswith("@example.com>")

    with mod.LocalFunnel(db) as restarted:
        replay_run = restarted.begin_batch_run(
            channel="email", max_actions=5, started_at="2026-07-16T10:01:00+00:00"
        )
        replay = restarted.prepare_email_intent(
            run_id=replay_run, now="2026-07-16T10:01:01+00:00", **kwargs
        )
        assert replay.created is False
        assert replay.intent_id == first.intent_id
        assert replay.message_id == first.message_id
        with pytest.raises(mod.ActionIntentConflict):
            restarted.prepare_email_intent(
                run_id=replay_run,
                now="2026-07-16T10:01:02+00:00",
                **{**kwargs, "body": kwargs["body"] + " Changed after reservation."},
            )
        with pytest.raises(ValueError, match="verified"):
            restarted.prepare_email_intent(
                run_id=replay_run,
                now="2026-07-16T10:01:03+00:00",
                **{
                    **kwargs,
                    "recipient": "unverified@example.net",
                    "recipient_verified": False,
                    "vacancy_key": "example-net:product-lead",
                },
            )


def test_sent_readback_closes_ambiguous_email_once_by_exact_message_id(tmp_path):
    mod = load_module()
    db = tmp_path / "funnel.sqlite3"
    kwargs = {
        "sender": "candidate@example.com",
        "recipient": "recruiter@example.org",
        "recipient_verified": True,
        "recipient_provenance": "company careers contact page",
        "vacancy_key": "example-org:ai-product-manager",
        "subject": "AI Product Manager — Alexander Shamshurin",
        "body": "Hello. A relevant product automation case is attached. Would a short conversation be useful?",
    }
    with mod.LocalFunnel(db) as funnel:
        run_id = funnel.begin_batch_run(
            channel="email", max_actions=5, started_at="2026-07-16T10:00:00+00:00"
        )
        email_intent = funnel.prepare_email_intent(
            run_id=run_id, now="2026-07-16T10:00:01+00:00", **kwargs
        )
        execution_token = funnel.mark_intent_executing(
            intent_id=email_intent.intent_id,
            worker_id="smtp-a",
            now="2026-07-16T10:00:02+00:00",
        )
        funnel.mark_intent_ambiguous(
            intent_id=email_intent.intent_id,
            execution_token=execution_token,
            now="2026-07-16T10:00:03+00:00",
            error_code="smtp_accepted_but_client_disconnected",
        )
        with pytest.raises(ValueError, match="Message-ID"):
            funnel.record_email_sent_readback(
                intent_id=email_intent.intent_id,
                message_id="<different@example.com>",
                sent_uid="imap-99",
                sent_at="2026-07-16T10:00:04+00:00",
                evidence_ref="imap://Sent/99",
            )
        receipt_id = funnel.record_email_sent_readback(
            intent_id=email_intent.intent_id,
            message_id=email_intent.message_id,
            sent_uid="imap-100",
            sent_at="2026-07-16T10:00:05+00:00",
            evidence_ref="imap://Sent/100",
        )

    with mod.LocalFunnel(db) as restarted:
        replay_id = restarted.record_email_sent_readback(
            intent_id=email_intent.intent_id,
            message_id=email_intent.message_id,
            sent_uid="imap-100",
            sent_at="2026-07-16T10:00:05+00:00",
            evidence_ref="imap://Sent/100",
        )
        assert replay_id == receipt_id

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    intent = con.execute("SELECT * FROM action_intents WHERE id=?", (email_intent.intent_id,)).fetchone()
    receipts = con.execute("SELECT * FROM email_receipts").fetchall()
    assert intent["state"] == "verified"
    assert len(receipts) == 1
    assert receipts[0]["run_id"] == run_id
    assert receipts[0]["action_intent_id"] == email_intent.intent_id
    assert receipts[0]["message_id"] == email_intent.message_id
    assert receipts[0]["sent_uid"] == "imap-100"
    assert receipts[0]["read_back_verified"] == 1


def test_email_followup_is_single_due_only_and_cancelled_by_reply(tmp_path):
    mod = load_module()
    db = tmp_path / "funnel.sqlite3"

    def verified_initial(funnel, suffix: str):
        run_id = funnel.begin_batch_run(
            channel="email", max_actions=5, started_at=f"2026-07-{suffix}T10:00:00+00:00"
        )
        intent = funnel.prepare_email_intent(
            run_id=run_id,
            sender="candidate@example.com",
            recipient=f"recruiter{suffix}@example.org",
            recipient_verified=True,
            recipient_provenance="official vacancy contact block",
            vacancy_key=f"example-org:pm-{suffix}",
            subject="Product Manager — candidate",
            body="Hello. A relevant case is attached.",
            now=f"2026-07-{suffix}T10:00:01+00:00",
        )
        token = funnel.mark_intent_executing(
            intent_id=intent.intent_id, worker_id="smtp", now=f"2026-07-{suffix}T10:00:02+00:00"
        )
        funnel.mark_intent_ambiguous(
            intent_id=intent.intent_id,
            execution_token=token,
            now=f"2026-07-{suffix}T10:00:03+00:00",
            error_code="reconcile_required",
        )
        funnel.record_email_sent_readback(
            intent_id=intent.intent_id,
            message_id=intent.message_id,
            sent_uid=f"uid-{suffix}",
            sent_at=f"2026-07-{suffix}T10:00:04+00:00",
            evidence_ref=f"imap://Sent/uid-{suffix}",
        )
        return intent

    with mod.LocalFunnel(db) as funnel:
        first = verified_initial(funnel, "16")
        followup_id = funnel.schedule_email_followup(
            initial_intent_id=first.intent_id,
            due_at="2026-07-21T10:00:04+00:00",
            now="2026-07-16T10:00:05+00:00",
        )
        assert funnel.schedule_email_followup(
            initial_intent_id=first.intent_id,
            due_at="2026-07-21T10:00:04+00:00",
            now="2026-07-16T10:00:06+00:00",
        ) == followup_id
        assert funnel.list_due_email_followups(now="2026-07-21T10:00:03+00:00") == []
        due = funnel.list_due_email_followups(now="2026-07-21T10:00:04+00:00")
        assert [x["id"] for x in due] == [followup_id]
        funnel.mark_email_response(
            initial_intent_id=first.intent_id,
            response_uid="inbox-77",
            responded_at="2026-07-20T09:00:00+00:00",
        )
        assert funnel.list_due_email_followups(now="2026-07-22T10:00:00+00:00") == []

        second = verified_initial(funnel, "17")
        second_followup = funnel.schedule_email_followup(
            initial_intent_id=second.intent_id,
            due_at="2026-07-22T10:00:04+00:00",
            now="2026-07-17T10:00:05+00:00",
        )
        assert [x["id"] for x in funnel.list_due_email_followups(
            now="2026-07-22T10:00:04+00:00", limit=5
        )] == [second_followup]


def test_due_followup_becomes_one_threaded_deterministic_intent(tmp_path):
    mod = load_module()
    db = tmp_path / "funnel.sqlite3"
    with mod.LocalFunnel(db) as funnel:
        initial_run = funnel.begin_batch_run(channel="email", max_actions=5, started_at="2026-07-16T10:00:00+00:00")
        initial = funnel.prepare_email_intent(
            run_id=initial_run, sender="candidate@example.com", recipient="recruiter@example.org",
            recipient_verified=True, recipient_provenance="official vacancy contact block",
            vacancy_key="example:pm", subject="Product Manager — candidate",
            body="Initial message.", now="2026-07-16T10:00:01+00:00",
        )
        token = funnel.mark_intent_executing(intent_id=initial.intent_id, worker_id="smtp", now="2026-07-16T10:00:02+00:00")
        funnel.mark_intent_ambiguous(intent_id=initial.intent_id, execution_token=token, now="2026-07-16T10:00:03+00:00", error_code="reconcile")
        funnel.record_email_sent_readback(
            intent_id=initial.intent_id, message_id=initial.message_id, sent_uid="100",
            sent_at="2026-07-16T10:00:04+00:00", evidence_ref="imap://Sent/100",
        )
        followup_id = funnel.schedule_email_followup(
            initial_intent_id=initial.intent_id, due_at="2026-07-21T10:00:04+00:00",
            now="2026-07-16T10:00:05+00:00",
        )
        followup_run = funnel.begin_batch_run(channel="email", max_actions=5, started_at="2026-07-21T10:00:04+00:00")
        first = funnel.prepare_due_email_followup(
            followup_id=followup_id, run_id=followup_run,
            body="Following up once in case the role is still relevant.",
            now="2026-07-21T10:00:05+00:00",
        )
        again = funnel.prepare_due_email_followup(
            followup_id=followup_id, run_id=followup_run,
            body="Following up once in case the role is still relevant.",
            now="2026-07-21T10:00:06+00:00",
        )
        assert again.intent_id == first.intent_id
        assert again.message_id == first.message_id
        assert first.created is True
        assert again.created is False
        payload = funnel.get_action_intent(intent_id=first.intent_id)["payload"]
        assert payload["in_reply_to"] == initial.message_id
        assert payload["references"] == initial.message_id
        assert payload["subject"] == "Re: Product Manager — candidate"
        assert payload["message_id"] != initial.message_id
        with pytest.raises(mod.ActionIntentConflict):
            funnel.prepare_due_email_followup(
                followup_id=followup_id, run_id=followup_run,
                body="Changed follow-up body.", now="2026-07-21T10:00:07+00:00",
            )
        followup_token = funnel.mark_intent_executing(
            intent_id=first.intent_id, worker_id="smtp-followup",
            now="2026-07-21T10:00:08+00:00",
        )
        funnel.mark_intent_ambiguous(
            intent_id=first.intent_id, execution_token=followup_token,
            now="2026-07-21T10:00:09+00:00", error_code="reconcile",
        )
        funnel.record_email_sent_readback(
            intent_id=first.intent_id, message_id=first.message_id,
            sent_uid="101", sent_at="2026-07-21T10:00:10+00:00",
            evidence_ref="imap://Sent/101",
        )
    con = sqlite3.connect(db)
    assert con.execute("SELECT state FROM email_followups WHERE id=?", (followup_id,)).fetchone()[0] == "verified"


def test_cli_imports_latest_files_and_prints_json(tmp_path, monkeypatch, capsys):
    mod = load_module()
    source = tmp_path / "rows.json"
    source.write_text(json.dumps([row()]), encoding="utf-8")
    db = tmp_path / "funnel.sqlite3"
    monkeypatch.setattr(sys, "argv", ["local_funnel.py", "--db", str(db), "import-json", str(source)])
    assert mod.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["accepted"] == 1
    assert payload["summary"]["jobs"] == 1
