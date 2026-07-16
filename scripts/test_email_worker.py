from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_local_funnel():
    path = ROOT / "scripts" / "local_funnel.py"
    spec = importlib.util.spec_from_file_location("local_funnel_email_worker_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    sys.modules["local_funnel"] = module
    spec.loader.exec_module(module)
    return module


def load_worker():
    path = ROOT / "scripts" / "email_worker.py"
    spec = importlib.util.spec_from_file_location("email_worker_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeTransport:
    def __init__(self, error: Exception | None = None):
        self.messages: list[bytes] = []
        self.error = error

    def send(self, raw_message: bytes) -> None:
        self.messages.append(raw_message)
        if self.error:
            raise self.error


class FakeSentStore:
    def __init__(self, found: dict | None):
        self.found = found
        self.lookups: list[str] = []

    def find_exact_message_id(self, message_id: str):
        self.lookups.append(message_id)
        return self.found


def prepare(funnel, mod):
    run_id = funnel.begin_batch_run(
        channel="email", max_actions=5, started_at="2026-07-16T11:00:00+00:00"
    )
    intent = funnel.prepare_email_intent(
        run_id=run_id,
        sender="candidate@example.com",
        recipient="recruiter@example.org",
        recipient_verified=True,
        recipient_provenance="company careers contact page",
        vacancy_key="example-org:ai-product-manager",
        subject="AI Product Manager — candidate",
        body="Hello. A relevant automation case is attached. Would a short conversation be useful?",
        now="2026-07-16T11:00:01+00:00",
    )
    return run_id, intent


def test_email_worker_sends_once_and_verifies_exact_sent_message_id(tmp_path):
    mod = load_local_funnel()
    worker = load_worker()
    with mod.LocalFunnel(tmp_path / "funnel.sqlite3") as funnel:
        run_id, intent = prepare(funnel, mod)
        transport = FakeTransport()
        sent = FakeSentStore(
            {
                "uid": "imap-101",
                "sent_at": "2026-07-16T11:00:03+00:00",
                "evidence_ref": "imap://Sent/101",
            }
        )
        result = worker.execute_email_intent(
            funnel=funnel,
            intent_id=intent.intent_id,
            worker_id="email-worker-a",
            transport=transport,
            sent_store=sent,
            now="2026-07-16T11:00:02+00:00",
        )
        assert result["status"] == "verified"
        assert result["run_id"] == run_id
        assert len(transport.messages) == 1
        raw = transport.messages[0].decode("utf-8")
        assert f"Message-ID: {intent.message_id}" in raw
        assert "To: recruiter@example.org" in raw
        assert sent.lookups == [intent.message_id]


def test_email_worker_crash_becomes_ambiguous_and_never_resends(tmp_path):
    mod = load_local_funnel()
    worker = load_worker()
    db = tmp_path / "funnel.sqlite3"
    with mod.LocalFunnel(db) as funnel:
        _, intent = prepare(funnel, mod)
        transport = FakeTransport(ConnectionError("lost after DATA"))
        sent = FakeSentStore(None)
        result = worker.execute_email_intent(
            funnel=funnel,
            intent_id=intent.intent_id,
            worker_id="email-worker-a",
            transport=transport,
            sent_store=sent,
            now="2026-07-16T11:00:02+00:00",
        )
        assert result["status"] == "ambiguous"
        assert len(transport.messages) == 1

    with mod.LocalFunnel(db) as restarted:
        with pytest.raises(mod.IntentFenceViolation):
            worker.execute_email_intent(
                funnel=restarted,
                intent_id=intent.intent_id,
                worker_id="email-worker-b",
                transport=transport,
                sent_store=sent,
                now="2026-07-16T11:01:00+00:00",
            )
    assert len(transport.messages) == 1


def test_restart_reconciles_ambiguous_email_without_smtp_resend(tmp_path):
    mod = load_local_funnel()
    worker = load_worker()
    db = tmp_path / "funnel.sqlite3"
    transport = FakeTransport(ConnectionError("lost after DATA"))
    with mod.LocalFunnel(db) as funnel:
        _, intent = prepare(funnel, mod)
        assert worker.execute_email_intent(
            funnel=funnel, intent_id=intent.intent_id, worker_id="worker-a",
            transport=transport, sent_store=FakeSentStore(None),
            now="2026-07-16T11:00:02+00:00",
        )["status"] == "ambiguous"
    with mod.LocalFunnel(db) as restarted:
        result = worker.reconcile_email_intent(
            funnel=restarted, intent_id=intent.intent_id,
            sent_store=FakeSentStore({"uid": "imap-102", "sent_at": "2026-07-16T11:00:03+00:00", "evidence_ref": "imap://Sent/102"}),
        )
    assert result["status"] == "verified"
    assert len(transport.messages) == 1


def test_verified_reply_cancels_pending_followup_idempotently(tmp_path):
    mod = load_local_funnel()
    worker = load_worker()
    with mod.LocalFunnel(tmp_path / "funnel.sqlite3") as funnel:
        _, intent = prepare(funnel, mod)
        token = funnel.mark_intent_executing(intent_id=intent.intent_id, worker_id="smtp", now="2026-07-16T11:00:02+00:00")
        funnel.mark_intent_ambiguous(intent_id=intent.intent_id, execution_token=token, now="2026-07-16T11:00:03+00:00", error_code="reconcile")
        funnel.record_email_sent_readback(intent_id=intent.intent_id, message_id=intent.message_id, sent_uid="sent-1", sent_at="2026-07-16T11:00:04+00:00", evidence_ref="imap://Sent/1")
        funnel.schedule_email_followup(initial_intent_id=intent.intent_id, due_at="2026-07-21T11:00:04+00:00", now="2026-07-16T11:00:05+00:00")
        class ReplyStore:
            def find_reply_to(self, message_id):
                assert message_id == intent.message_id
                return {"uid": "inbox-77", "responded_at": "2026-07-18T09:00:00+00:00", "evidence_ref": "imap://INBOX/77"}
        first = worker.reconcile_email_response(funnel=funnel, initial_intent_id=intent.intent_id, reply_store=ReplyStore())
        second = worker.reconcile_email_response(funnel=funnel, initial_intent_id=intent.intent_id, reply_store=ReplyStore())
        assert first["status"] == second["status"] == "response_verified"
        assert funnel.list_due_email_followups(now="2026-07-22T00:00:00+00:00") == []
