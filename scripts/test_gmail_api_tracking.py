from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module():
    path = ROOT / "scripts" / "gmail_api_tracking.py"
    spec = importlib.util.spec_from_file_location("gmail_api_tracking_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def message(sender, subject, message_id, *, labels=(), internal_date="1784240000000"):
    return {
        "id": message_id,
        "internalDate": internal_date,
        "labelIds": list(labels),
        "payload": {"headers": [
            {"name": "From", "value": sender},
            {"name": "Subject", "value": subject},
        ]},
    }


def test_classifies_external_thread_message_as_reply():
    mod = load_module()
    thread = {"messages": [
        message("Alexander <alex2003mail.ru@gmail.com>", "AI Product Maker", "sent-1", labels=["SENT"]),
        message("Recruiter <join@paralect.com>", "Re: AI Product Maker", "reply-1", labels=["INBOX"]),
    ]}
    result = mod.classify_thread(thread, own_email="alex2003mail.ru@gmail.com")
    assert result["status"] == "reply"
    assert result["response_message_id"] == "reply-1"
    assert result["responder"] == "join@paralect.com"


def test_classifies_delivery_status_notification_as_bounce():
    mod = load_module()
    thread = {"messages": [
        message("Alexander <alex2003mail.ru@gmail.com>", "AI Product Maker", "sent-1", labels=["SENT"]),
        message("Mail Delivery Subsystem <mailer-daemon@googlemail.com>", "Delivery Status Notification (Failure)", "bounce-1", labels=["INBOX"]),
    ]}
    result = mod.classify_thread(thread, own_email="alex2003mail.ru@gmail.com")
    assert result["status"] == "bounce"
    assert result["response_message_id"] == "bounce-1"


def test_ignores_own_messages_and_reports_no_response():
    mod = load_module()
    thread = {"messages": [message("Alexander <alex2003mail.ru@gmail.com>", "AI Product Maker", "sent-1", labels=["SENT"])]}
    assert mod.classify_thread(thread, own_email="alex2003mail.ru@gmail.com")["status"] == "no_response"


def test_summary_separates_awaiting_replies_from_bounces():
    mod = load_module()
    summary = mod.summarize([
        {"status": "reply"}, {"status": "bounce"},
        {"status": "no_response"}, {"status": "no_response"},
    ])
    assert summary == {"tracked": 4, "replied": 1, "bounced": 1, "awaiting": 2, "reply_rate": 0.25, "bounce_rate": 0.25}
