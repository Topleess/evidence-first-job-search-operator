from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from linkedin_easy_apply_adapter import DurableIntentStore, IntentFenceViolation


def prepared(store: DurableIntentStore):
    return store.reserve(
        job_id="4430000001",
        job_url="https://www.linkedin.com/jobs/view/4430000001/",
        form_fingerprint="form-v1",
        payload={"mode": "submit", "fills_digest": "abc"},
    )


def test_submit_requires_current_execution_token(tmp_path: Path):
    store = DurableIntentStore(tmp_path / "agent-linkedin.sqlite3")
    intent = prepared(store)
    token = store.begin_submit(intent.intent_id, worker_id="linkedin-agent")
    with pytest.raises(IntentFenceViolation, match="stale execution token"):
        store.record_submit_readback(
            intent.intent_id,
            execution_token="wrong",
            readback={"marker": "application_submitted", "job_id": "4430000001"},
            evidence_bytes=b"confirmed page",
        )
    assert store.get(intent.intent_id)["state"] == "submitting"
    assert token


def test_submit_without_verified_readback_becomes_ambiguous_and_has_no_receipt(tmp_path: Path):
    store = DurableIntentStore(tmp_path / "agent-linkedin.sqlite3")
    intent = prepared(store)
    token = store.begin_submit(intent.intent_id, worker_id="linkedin-agent")
    store.mark_submit_ambiguous(intent.intent_id, execution_token=token, reason="navigation_timeout")
    assert store.get(intent.intent_id)["state"] == "ambiguous"
    assert store.list_receipts() == []


def test_verified_readback_creates_privacy_safe_receipt_once(tmp_path: Path):
    store = DurableIntentStore(tmp_path / "agent-linkedin.sqlite3")
    intent = prepared(store)
    token = store.begin_submit(intent.intent_id, worker_id="linkedin-agent")
    evidence = b"Application submitted for job 4430000001"
    receipt_id = store.record_submit_readback(
        intent.intent_id,
        execution_token=token,
        readback={"marker": "application_submitted", "job_id": "4430000001"},
        evidence_bytes=evidence,
    )
    row = store.list_receipts()[0]
    assert row["id"] == receipt_id
    assert row["intent_id"] == intent.intent_id
    assert row["job_id"] == "4430000001"
    assert row["evidence_sha256"] == hashlib.sha256(evidence).hexdigest()
    assert row["payload_digest"]
    serialized = json.dumps(row).lower()
    assert "alexander" not in serialized
    assert "email" not in serialized
    assert "phone" not in serialized
    assert store.get(intent.intent_id)["state"] == "verified"

    same = store.record_submit_readback(
        intent.intent_id,
        execution_token=token,
        readback={"marker": "application_submitted", "job_id": "4430000001"},
        evidence_bytes=evidence,
    )
    assert same == receipt_id
    assert len(store.list_receipts()) == 1


def test_readback_must_match_intent_job_and_known_marker(tmp_path: Path):
    store = DurableIntentStore(tmp_path / "agent-linkedin.sqlite3")
    intent = prepared(store)
    token = store.begin_submit(intent.intent_id, worker_id="linkedin-agent")
    with pytest.raises(ValueError, match="job identity"):
        store.record_submit_readback(
            intent.intent_id,
            execution_token=token,
            readback={"marker": "application_submitted", "job_id": "999"},
            evidence_bytes=b"x",
        )
    with pytest.raises(ValueError, match="verified submission marker"):
        store.record_submit_readback(
            intent.intent_id,
            execution_token=token,
            readback={"marker": "button_clicked", "job_id": "4430000001"},
            evidence_bytes=b"x",
        )
