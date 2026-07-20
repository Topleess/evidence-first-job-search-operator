import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from hh_cron_runner import finish_run, persist_terminal_results, recover_stale_batches, recover_stale_executing, select_candidates, summarize_results
from local_funnel import LocalFunnel


def enqueue(db, external_id, eligibility):
    if eligibility is not None:
        eligibility = {**eligibility, "evidence_vacancy_id": external_id}
    payload = {
        "source": "hh", "external_id": external_id,
        "job_url": f"https://hh.kz/vacancy/{external_id}",
        "job_title": "Product Manager", "company": "Example",
        "eligibility": eligibility,
    }
    with sqlite3.connect(db) as con:
        con.execute("""INSERT INTO queue(kind,payload,idempotency_key,state,available_at,max_attempts)
                     VALUES('application',?,?, 'pending','2026-07-17T10:00:00+00:00',3)""",
                    (json.dumps(payload), f"hh:{external_id}"))


def test_selects_only_live_eligible_and_never_retries_ambiguous_intent(tmp_path):
    db = tmp_path / "funnel.sqlite3"
    funnel = LocalFunnel(path=db)
    good = {"eligible": True, "checked_at": "2026-07-17T09:00:00Z", "evidence": "live vacancy page reviewed"}
    enqueue(db, "100", good)
    enqueue(db, "101", None)
    enqueue(db, "102", {**good, "eligible": False})

    chosen = select_candidates(db, now=datetime(2026, 7, 17, 10, tzinfo=timezone.utc), daily_cap=20, batch_limit=5)
    assert [x["source_job_id"] for x in chosen] == ["100"]

    now = datetime(2026, 7, 17, 10, tzinfo=timezone.utc)
    run = funnel.begin_batch_run(channel="hh", max_actions=5, started_at=now)
    reservation = funnel.reserve_action_intent(run_id=run, kind="application_submit", idempotency_key="hh:100:application", payload={"source": "hh", "daily_cap": 20, "external_id": "100"}, now=now)
    token = funnel.mark_intent_executing(intent_id=reservation.intent_id, worker_id="test", now=now)
    funnel.mark_intent_ambiguous(intent_id=reservation.intent_id, execution_token=token, error_code="submit_outcome_unknown", now=now)

    assert select_candidates(db, now=datetime(2026, 7, 17, 10, tzinfo=timezone.utc), daily_cap=20, batch_limit=5) == []


def test_legacy_hh_ambiguous_payload_is_also_deduplicated(tmp_path):
    db = tmp_path / "funnel.sqlite3"
    funnel = LocalFunnel(path=db)
    good = {"eligible": True, "checked_at": "2026-07-17T09:00:00Z", "evidence": "live vacancy page reviewed"}
    enqueue(db, "777", good)
    now = datetime(2026, 7, 17, 10, tzinfo=timezone.utc)
    run = funnel.begin_batch_run(channel="hh", max_actions=1, started_at=now)
    reservation = funnel.reserve_action_intent(
        run_id=run, kind="application_submit", idempotency_key="legacy-hh-777",
        payload={"vacancy_id": "777", "url": "https://hh.ru/vacancy/777", "daily_cap": 20}, now=now,
    )
    token = funnel.mark_intent_executing(intent_id=reservation.intent_id, worker_id="legacy", now=now)
    funnel.mark_intent_ambiguous(intent_id=reservation.intent_id, execution_token=token, error_code="legacy_unknown", now=now)

    assert select_candidates(db, now=now, daily_cap=20, batch_limit=1) == []


def test_candidate_limits_must_be_positive(tmp_path):
    db = tmp_path / "funnel.sqlite3"
    LocalFunnel(path=db)
    import pytest
    now = datetime(2026, 7, 17, 10, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        select_candidates(db, now=now, daily_cap=20, batch_limit=0)
    with pytest.raises(ValueError):
        select_candidates(db, now=now, daily_cap=0, batch_limit=1)


def test_finish_run_closes_running_batch_with_executor_outcome(tmp_path):
    db = tmp_path / "funnel.sqlite3"
    funnel = LocalFunnel(path=db)
    now = datetime(2026, 7, 17, 10, tzinfo=timezone.utc)
    run_id = funnel.begin_batch_run(channel="hh", max_actions=5, started_at=now)

    finish_run(funnel, run_id, exit_code=2, now=now)

    with sqlite3.connect(db) as con:
        row = con.execute("SELECT state,stop_reason FROM batch_runs WHERE id=?", (run_id,)).fetchone()
    assert row == ("failed", "executor_exit_2")


def test_recover_stale_executing_marks_ambiguous_without_replay(tmp_path):
    db = tmp_path / "funnel.sqlite3"
    funnel = LocalFunnel(path=db)
    old = datetime(2026, 7, 17, 9, tzinfo=timezone.utc)
    run = funnel.begin_batch_run(channel="hh", max_actions=1, started_at=old)
    reservation = funnel.reserve_action_intent(
        run_id=run, kind="application_submit", idempotency_key="hh:200:application",
        payload={"source": "hh", "daily_cap": 20, "external_id": "200"}, now=old,
    )
    funnel.mark_intent_executing(intent_id=reservation.intent_id, worker_id="crashed", now=old)

    recovered = recover_stale_executing(db, now=datetime(2026, 7, 17, 10, tzinfo=timezone.utc), stale_seconds=1800)

    assert recovered == 1
    with sqlite3.connect(db) as con:
        row = con.execute("SELECT state,last_error_code,execution_token FROM action_intents WHERE id=?", (reservation.intent_id,)).fetchone()
    assert row == ("ambiguous", "stale_executing_requires_readback", None)


def test_summarize_results_reports_every_nonterminal_or_missing_item():
    selected = [{"source_job_id": "1"}, {"source_job_id": "2"}, {"source_job_id": "3"}]
    summary = summarize_results(selected, [
        {"id": "1", "status": "verified", "submitted": True, "read_back_verified": True, "receipt_id": 1},
        {"id": "2", "status": "error_before_submit", "error": "network"},
    ])
    assert summary["verified"] == 1
    assert [x["id"] for x in summary["blockers"]] == ["2", "3"]
    assert summary["blockers"][1]["status"] == "missing_result"


def test_summarize_results_rejects_unproven_terminal_claims():
    selected = [{"source_job_id": str(i)} for i in range(1, 5)]
    summary = summarize_results(selected, [
        {"id": "1", "status": "verified"},
        {"id": "2", "status": "duplicate", "submitted": True, "read_back_verified": False},
        {"id": "3", "status": "dry_run_ready"},
        {"id": "4", "status": "verified", "submitted": True, "read_back_verified": True, "receipt_id": 7},
    ])
    assert summary["verified"] == 1
    assert [item["id"] for item in summary["blockers"]] == ["1", "2", "3"]


def test_stale_batch_blocks_reserved_intent_and_closes_run(tmp_path):
    db = tmp_path / "funnel.sqlite3"
    with LocalFunnel(path=db) as funnel:
        run_id = funnel.begin_batch_run(channel="hh", max_actions=1, started_at="2026-07-17T09:00:00+00:00")
        intent = funnel.reserve_action_intent(
            run_id=run_id, kind="application_submit", idempotency_key="hh:stale:application",
            payload={"source": "hh", "daily_cap": 20, "external_id": "900"}, now="2026-07-17T09:00:01+00:00",
        )
    batches, reserved = recover_stale_batches(db, now=datetime(2026, 7, 17, 10, tzinfo=timezone.utc), stale_seconds=1800)
    assert (batches, reserved) == (1, 1)
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT state FROM batch_runs WHERE id=?", (run_id,)).fetchone()[0] == "failed"
        assert con.execute("SELECT state,last_error_code FROM action_intents WHERE id=?", (intent.intent_id,)).fetchone() == ("blocked", "stale_reserved_batch_closed")


def test_persistence_does_not_close_malformed_claims_or_dry_run(tmp_path):
    db = tmp_path / "funnel.sqlite3"
    funnel = LocalFunnel(path=db)
    good = {"eligible": True, "checked_at": "2026-07-17T09:00:00Z", "evidence": "live"}
    for vacancy_id in ("501", "502", "503"):
        enqueue(db, vacancy_id, good)
    selected = select_candidates(db, now=datetime(2026, 7, 17, 10, tzinfo=timezone.utc), daily_cap=20, batch_limit=5)
    persist_terminal_results(funnel, db, selected, [
        {"id": "501", "status": "verified"},
        {"id": "502", "status": "duplicate", "submitted": True, "read_back_verified": False},
        {"id": "503", "status": "dry_run_ready", "dry_run": True},
    ], now=datetime(2026, 7, 17, 10, tzinfo=timezone.utc))
    with sqlite3.connect(db) as con:
        states = dict(con.execute("SELECT json_extract(payload,'$.external_id'),state FROM queue"))
    assert states == {"501": "failed", "502": "failed", "503": "pending"}
