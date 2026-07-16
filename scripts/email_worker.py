#!/usr/bin/env python3
"""Fail-closed email side-effect worker.

The worker never retries an ambiguous SMTP result. It first persists an
execution fence, performs one transport call, marks the result ambiguous, and
then verifies the exact deterministic Message-ID through a Sent-store adapter.
"""
from __future__ import annotations

from email.message import EmailMessage
from email.policy import SMTP
from typing import Any, Protocol

from local_funnel import IntentFenceViolation, LocalFunnel, parse_time


class EmailTransport(Protocol):
    def send(self, raw_message: bytes) -> None: ...


class SentStore(Protocol):
    def find_exact_message_id(self, message_id: str) -> dict[str, Any] | None: ...


class ReplyStore(Protocol):
    def find_reply_to(self, message_id: str) -> dict[str, Any] | None: ...


def _verified_sent_receipt(
    *, funnel: LocalFunnel, intent: dict[str, Any], found: dict[str, Any]
) -> dict[str, Any]:
    for key in ("uid", "sent_at", "evidence_ref"):
        if not found.get(key):
            raise ValueError(f"Sent-store result missing {key}")
    receipt_id = funnel.record_email_sent_readback(
        intent_id=intent["id"], message_id=intent["payload"]["message_id"],
        sent_uid=str(found["uid"]), sent_at=found["sent_at"],
        evidence_ref=str(found["evidence_ref"]),
    )
    return {"status": "verified", "intent_id": intent["id"],
            "run_id": intent["run_id"], "message_id": intent["payload"]["message_id"],
            "receipt_id": receipt_id}


def reconcile_email_intent(*, funnel: LocalFunnel, intent_id: int, sent_store: SentStore) -> dict[str, Any]:
    """Read-only reconciliation path; deliberately has no transport argument."""
    intent = funnel.get_action_intent(intent_id=intent_id)
    if intent["kind"] not in {"email_send", "email_followup"}:
        raise ValueError("intent is not an email side effect")
    if intent["state"] not in {"ambiguous", "verified"}:
        raise IntentFenceViolation("email intent is not reconcilable")
    found = sent_store.find_exact_message_id(intent["payload"]["message_id"])
    if found is None:
        return {"status": "ambiguous", "intent_id": intent_id,
                "run_id": intent["run_id"], "message_id": intent["payload"]["message_id"]}
    return _verified_sent_receipt(funnel=funnel, intent=intent, found=found)


def reconcile_email_response(*, funnel: LocalFunnel, initial_intent_id: int, reply_store: ReplyStore) -> dict[str, Any]:
    intent = funnel.get_action_intent(intent_id=initial_intent_id)
    if intent["kind"] != "email_send" or intent["state"] != "verified":
        raise IntentFenceViolation("response reconciliation requires a verified initial email")
    found = reply_store.find_reply_to(intent["payload"]["message_id"])
    if found is None:
        return {"status": "no_response", "intent_id": initial_intent_id}
    for key in ("uid", "responded_at", "evidence_ref"):
        if not found.get(key):
            raise ValueError(f"Reply-store result missing {key}")
    funnel.mark_email_response(initial_intent_id=initial_intent_id,
                               response_uid=str(found["uid"]), responded_at=found["responded_at"])
    return {"status": "response_verified", "intent_id": initial_intent_id,
            "response_uid": str(found["uid"]), "evidence_ref": str(found["evidence_ref"])}


def build_mime(payload: dict[str, Any], *, now: str) -> bytes:
    required = {"sender", "recipient", "subject", "body", "message_id"}
    if not required.issubset(payload):
        raise ValueError("email intent payload is incomplete")
    message = EmailMessage(policy=SMTP)
    message["From"] = payload["sender"]
    message["To"] = payload["recipient"]
    message["Subject"] = payload["subject"]
    message["Message-ID"] = payload["message_id"]
    if payload.get("in_reply_to"):
        message["In-Reply-To"] = payload["in_reply_to"]
    if payload.get("references"):
        message["References"] = payload["references"]
    message["Date"] = parse_time(now)
    message.set_content(payload["body"], subtype="plain", charset="utf-8")
    return message.as_bytes(policy=SMTP)


def execute_email_intent(
    *,
    funnel: LocalFunnel,
    intent_id: int,
    worker_id: str,
    transport: EmailTransport,
    sent_store: SentStore,
    now: str,
) -> dict[str, Any]:
    intent = funnel.get_action_intent(intent_id=intent_id)
    if intent["kind"] not in {"email_send", "email_followup"}:
        raise ValueError("intent is not an email side effect")
    payload = intent["payload"]
    token = funnel.mark_intent_executing(
        intent_id=intent_id, worker_id=worker_id, now=now
    )
    raw_message = build_mime(payload, now=now)
    error_code = "smtp_returned_reconcile_required"
    try:
        transport.send(raw_message)
    except Exception as exc:
        error_code = f"smtp_uncertain_{type(exc).__name__.lower()}"[:128]
    funnel.mark_intent_ambiguous(
        intent_id=intent_id,
        execution_token=token,
        now=now,
        error_code=error_code,
    )
    found = sent_store.find_exact_message_id(payload["message_id"])
    if found is None:
        return {
            "status": "ambiguous",
            "intent_id": intent_id,
            "run_id": intent["run_id"],
            "message_id": payload["message_id"],
        }
    for key in ("uid", "sent_at", "evidence_ref"):
        if not found.get(key):
            raise ValueError(f"Sent-store result missing {key}")
    receipt_id = funnel.record_email_sent_readback(
        intent_id=intent_id,
        message_id=payload["message_id"],
        sent_uid=str(found["uid"]),
        sent_at=found["sent_at"],
        evidence_ref=str(found["evidence_ref"]),
    )
    return {
        "status": "verified",
        "intent_id": intent_id,
        "run_id": intent["run_id"],
        "message_id": payload["message_id"],
        "receipt_id": receipt_id,
    }
