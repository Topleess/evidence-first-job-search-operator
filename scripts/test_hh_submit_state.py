import json
import hashlib
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from hh_submit_state import reserve_hh, record_verified_hh
from local_funnel import IntentFenceViolation, LocalFunnel


def test_hh_bridge_can_close_execution_only_before_side_effect(tmp_path: Path):
    db = tmp_path / "funnel.sqlite3"
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with LocalFunnel(db) as funnel:
        run_id = funnel.begin_batch_run(channel="hh", max_actions=1, started_at=now)
        intent = reserve_hh(
            funnel, run_id=run_id,
            vacancy={"id": "124", "url": "https://hh.ru/vacancy/124", "company": "Acme", "title": "PM"},
            form_fingerprint="a" * 64, truth_map_sha256="b" * 64, plan_sha256="c" * 64,
            now=now.isoformat(),
        )
        token = funnel.mark_intent_executing(intent_id=intent.intent_id, worker_id="hh-browser", now=now)
        funnel.close_execution_blocked(
            intent_id=intent.intent_id, execution_token=token, now=now,
            error_code="final_submit_control_changed_before_click",
        )
        row = funnel.get_action_intent(intent_id=intent.intent_id)
        assert row["state"] == "blocked"
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT last_error_code FROM action_intents WHERE id=?", (intent.intent_id,)).fetchone()[0] == "final_submit_control_changed_before_click"


def test_hh_state_bridge_binds_fingerprint_truth_and_token(tmp_path: Path):
    db = tmp_path / "funnel.sqlite3"
    evidence = tmp_path / "readback.json"
    current = datetime.now(timezone.utc).replace(microsecond=0)
    started_at = (current - timedelta(minutes=2)).isoformat()
    submitted_at = (current - timedelta(minutes=1)).isoformat()

    with LocalFunnel(db) as funnel:
        run_id = funnel.begin_batch_run(channel="hh", max_actions=1, started_at=started_at)
        intent = reserve_hh(
            funnel, run_id=run_id, vacancy={"id": "123", "url": "https://hh.kz/vacancy/123", "company": "Acme", "title": "PM"},
            form_fingerprint="a" * 64, truth_map_sha256="b" * 64, plan_sha256="c" * 64,
            now=started_at,
        )
        token = funnel.mark_intent_executing(intent_id=intent.intent_id, worker_id="hh-browser", now=started_at)
        readback = "verified body"
        evidence.write_text(json.dumps({
            "id": "123", "url": "https://hh.kz/vacancy/123", "final_url": "https://hh.kz/vacancy/123",
            "marker": "already_applied_on_reopen", "observed_at": submitted_at,
            "intent_id": intent.intent_id, "execution_token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "readback_text": readback, "readback_text_sha256": hashlib.sha256(readback.encode()).hexdigest(),
            "form_fingerprint": "a" * 64, "truth_map_sha256": "b" * 64, "plan_sha256": "c" * 64,
        }), encoding="utf-8")
        forged = tmp_path / "forged.json"
        forged.write_text(json.dumps({"id": "123", "url": "https://evil.example/not-the-vacancy", "final_url": "https://evil.example/not-the-vacancy", "marker": "already_applied_on_reopen"}), encoding="utf-8")
        with pytest.raises((ValueError, OSError)):
            record_verified_hh(
                funnel, intent_id=intent.intent_id, execution_token=token,
                result={"id": "123", "url": "https://evil.example/not-the-vacancy", "evidence_path": str(forged)},
                submitted_at="not-a-time",
            )
        with pytest.raises(IntentFenceViolation):
            record_verified_hh(
                funnel, intent_id=intent.intent_id, execution_token="stale",
                result={"id": "123", "url": "https://hh.kz/vacancy/123", "company": "Acme", "title": "PM", "evidence_path": str(evidence)},
                submitted_at=submitted_at,
            )
        future_at = (current + timedelta(minutes=1)).isoformat()
        future_evidence = json.loads(evidence.read_text(encoding="utf-8"))
        future_evidence["observed_at"] = future_at
        evidence.write_text(json.dumps(future_evidence), encoding="utf-8")
        with pytest.raises(ValueError):
            record_verified_hh(
                funnel, intent_id=intent.intent_id, execution_token=token,
                result={"id": "123", "url": "https://hh.kz/vacancy/123", "company": "Acme", "title": "PM", "evidence_path": str(evidence)},
                submitted_at=future_at,
            )
        future_evidence["observed_at"] = submitted_at
        evidence.write_text(json.dumps(future_evidence), encoding="utf-8")
        receipt_id = record_verified_hh(
            funnel, intent_id=intent.intent_id, execution_token=token,
            result={"id": "123", "url": "https://hh.kz/vacancy/123", "company": "Acme", "title": "PM", "evidence_path": str(evidence)},
            submitted_at=submitted_at,
        )
    assert receipt_id > 0
