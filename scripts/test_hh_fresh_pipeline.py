import json
import sqlite3
from datetime import datetime, timedelta, timezone

from hh_cron_runner import select_candidates


def make_db(path):
    with sqlite3.connect(path) as con:
        con.executescript(
            """
            CREATE TABLE queue(id INTEGER PRIMARY KEY, kind TEXT, state TEXT, available_at TEXT, payload TEXT);
            CREATE TABLE application_receipts(id INTEGER PRIMARY KEY, source TEXT, external_vacancy_id TEXT, submitted INTEGER, read_back_verified INTEGER, submitted_at TEXT);
            CREATE TABLE action_intents(id INTEGER PRIMARY KEY, kind TEXT, state TEXT, payload TEXT, side_effect_maybe_at TEXT, execution_started_at TEXT, created_at TEXT);
            """
        )


def add_queue(path, *, queue_id, kind, available_at, external_id):
    gate = {
        "eligible": True,
        "checked_at": available_at,
        "evidence": f"/private/evidence/{external_id}.json",
        "evidence_vacancy_id": external_id,
    }
    payload = {
        "source": "hh",
        "external_id": external_id,
        "job_url": f"https://hh.ru/vacancy/{external_id}",
        "job_title": "AI Product Manager",
        "company": "Example",
        "eligibility": gate,
    }
    with sqlite3.connect(path) as con:
        con.execute(
            "INSERT INTO queue(id,kind,state,available_at,payload) VALUES (?,?,'pending',?,?)",
            (queue_id, kind, available_at, json.dumps(payload)),
        )


def test_selector_accepts_only_fresh_application_reviews(tmp_path):
    db = tmp_path / "funnel.sqlite3"
    make_db(db)
    now = datetime(2026, 7, 28, 14, tzinfo=timezone.utc)
    add_queue(db, queue_id=1, kind="application_review", available_at=(now - timedelta(hours=2)).isoformat(), external_id="1001")
    add_queue(db, queue_id=2, kind="application_review", available_at=(now - timedelta(hours=25)).isoformat(), external_id="1002")
    add_queue(db, queue_id=3, kind="email_outreach_draft", available_at=(now - timedelta(hours=1)).isoformat(), external_id="1003")

    chosen = select_candidates(db, now=now, daily_cap=3, batch_limit=3, fresh_ttl_hours=24)

    assert [item["source_job_id"] for item in chosen] == ["1001"]


def test_selector_daily_cap_still_blocks_after_verified_receipt(tmp_path):
    db = tmp_path / "funnel.sqlite3"
    make_db(db)
    now = datetime(2026, 7, 28, 14, tzinfo=timezone.utc)
    add_queue(db, queue_id=1, kind="application_review", available_at=(now - timedelta(hours=1)).isoformat(), external_id="1001")
    with sqlite3.connect(db) as con:
        con.execute(
            "INSERT INTO application_receipts(source,external_vacancy_id,submitted,read_back_verified,submitted_at) VALUES ('hh','999',1,1,?)",
            (now.isoformat(),),
        )

    assert select_candidates(db, now=now, daily_cap=1, batch_limit=1, fresh_ttl_hours=24) == []
