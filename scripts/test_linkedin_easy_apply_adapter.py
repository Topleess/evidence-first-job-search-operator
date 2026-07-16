from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from linkedin_easy_apply_adapter import (
    AuthState,
    DurableIntentStore,
    FieldSpec,
    FormClassifier,
    KnownProfile,
    UnsafeAction,
    classify_auth,
)


def test_auth_detection_distinguishes_authenticated_login_and_challenge():
    assert classify_auth("https://www.linkedin.com/jobs/view/123/", "Jobs\nEasy Apply") is AuthState.AUTHENTICATED
    assert classify_auth("https://www.linkedin.com/login", "Sign in") is AuthState.LOGIN_REQUIRED
    assert classify_auth("https://www.linkedin.com/checkpoint/challenge/", "Security verification") is AuthState.CHALLENGE


def test_classifier_fills_only_known_fields_and_blocks_unknown_required():
    profile = KnownProfile(first_name="Alexander", last_name="Shamshurin", email="a@example.com", phone="+79990000000")
    fields = [
        FieldSpec(key="first", label="First name", kind="text", required=True),
        FieldSpec(key="email", label="Email address", kind="email", required=True),
        FieldSpec(key="salary", label="Expected annual salary", kind="text", required=True),
        FieldSpec(key="optional", label="Portfolio", kind="text", required=False),
    ]
    result = FormClassifier(profile).classify(fields)
    assert result.fills == {"first": "Alexander", "email": "a@example.com"}
    assert [item.key for item in result.blockers] == ["salary"]
    assert result.ready_for_review is False


def test_submit_is_impossible_in_dry_run_adapter(tmp_path: Path):
    store = DurableIntentStore(tmp_path / "agent-linkedin.sqlite3")
    intent = store.reserve(job_id="123", job_url="https://www.linkedin.com/jobs/view/123/", form_fingerprint="abc", payload={"fills": {}})
    with pytest.raises(UnsafeAction, match="final Submit is disabled"):
        store.mark_submitted(intent.intent_id)


def test_durable_intent_is_idempotent_and_payload_fenced_across_restart(tmp_path: Path):
    db = tmp_path / "agent-linkedin.sqlite3"
    first = DurableIntentStore(db)
    one = first.reserve(job_id="123", job_url="https://www.linkedin.com/jobs/view/123/", form_fingerprint="abc", payload={"fills": {"first": "Alexander"}})
    first.close()

    second = DurableIntentStore(db)
    same = second.reserve(job_id="123", job_url="https://www.linkedin.com/jobs/view/123/", form_fingerprint="abc", payload={"fills": {"first": "Alexander"}})
    assert same.intent_id == one.intent_id
    assert same.created is False
    with pytest.raises(ValueError, match="payload conflict"):
        second.reserve(job_id="123", job_url="https://www.linkedin.com/jobs/view/123/", form_fingerprint="abc", payload={"fills": {"first": "Other"}})


def test_stale_form_fingerprint_creates_blocked_intent_not_reuses_old(tmp_path: Path):
    store = DurableIntentStore(tmp_path / "agent-linkedin.sqlite3")
    old = store.reserve(job_id="123", job_url="https://www.linkedin.com/jobs/view/123/", form_fingerprint="old", payload={"fills": {}})
    new = store.reserve(job_id="123", job_url="https://www.linkedin.com/jobs/view/123/", form_fingerprint="new", payload={"fills": {}})
    assert new.intent_id != old.intent_id
    assert store.get(old.intent_id)["state"] == "blocked_stale"
    assert store.get(new.intent_id)["state"] == "prepared"


def test_database_contains_no_production_receipt_and_records_evidence(tmp_path: Path):
    db = tmp_path / "agent-linkedin.sqlite3"
    store = DurableIntentStore(db)
    intent = store.reserve(job_id="999", job_url="https://www.linkedin.com/jobs/view/999/", form_fingerprint="fp", payload={"blockers": ["salary"]})
    store.record_dry_run(intent.intent_id, evidence={"auth": "authenticated", "stop_reason": "unknown_required"})
    row = store.get(intent.intent_id)
    assert row["state"] == "blocked_required"
    assert json.loads(row["evidence_json"])["stop_reason"] == "unknown_required"
    with sqlite3.connect(db) as con:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "application_receipts" not in tables
