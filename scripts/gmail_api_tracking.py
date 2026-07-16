#!/usr/bin/env python3
"""Track replies and delivery failures for verified Gmail API sends."""
from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Iterable

DEFAULT_TOKEN = Path("/opt/data/google_token.json")
DEFAULT_RECEIPTS = Path("/opt/data/job-search/outreach")
DEFAULT_REPORT = Path("/opt/data/job-search/state/gmail_outreach_status.json")
BOUNCE_SENDERS = {"mailer-daemon@googlemail.com", "mailer-daemon@gmail.com", "postmaster@google.com"}
BOUNCE_SUBJECT_MARKERS = ("delivery status notification", "undeliverable", "delivery failure", "mail delivery failed")


def _headers(message: dict[str, Any]) -> dict[str, str]:
    return {str(h.get("name", "")).lower(): str(h.get("value", ""))
            for h in message.get("payload", {}).get("headers", [])}


def classify_thread(thread: dict[str, Any], *, own_email: str) -> dict[str, Any]:
    own = own_email.strip().lower()
    messages = sorted(thread.get("messages", []), key=lambda item: int(item.get("internalDate", 0)))
    for message in reversed(messages):
        headers = _headers(message)
        sender = parseaddr(headers.get("from", ""))[1].lower()
        if not sender or sender == own or "SENT" in message.get("labelIds", []):
            continue
        subject = headers.get("subject", "")
        lowered = subject.lower()
        status = "bounce" if sender in BOUNCE_SENDERS or any(marker in lowered for marker in BOUNCE_SUBJECT_MARKERS) else "reply"
        return {
            "status": status,
            "response_message_id": str(message.get("id", "")),
            "responder": sender,
            "subject": subject,
            "received_at_ms": str(message.get("internalDate", "")),
        }
    return {"status": "no_response"}


def summarize(results: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(results)
    tracked = len(rows)
    replied = sum(row.get("status") == "reply" for row in rows)
    bounced = sum(row.get("status") == "bounce" for row in rows)
    awaiting = sum(row.get("status") == "no_response" for row in rows)
    return {
        "tracked": tracked, "replied": replied, "bounced": bounced, "awaiting": awaiting,
        "reply_rate": replied / tracked if tracked else 0.0,
        "bounce_rate": bounced / tracked if tracked else 0.0,
    }


def refresh_access_token(token_path: Path) -> tuple[str, str]:
    token = json.loads(token_path.read_text(encoding="utf-8"))
    form = urllib.parse.urlencode({
        "client_id": token["client_id"], "client_secret": token["client_secret"],
        "refresh_token": token["refresh_token"], "grant_type": "refresh_token",
    }).encode()
    with urllib.request.urlopen(urllib.request.Request(token["token_uri"], data=form), timeout=30) as response:
        access = json.load(response)["access_token"]
    account = str(token.get("account") or "alex2003mail.ru@gmail.com")
    return access, account


def gmail_thread(thread_id: str, access_token: str) -> dict[str, Any]:
    safe_id = urllib.parse.quote(thread_id, safe="")
    url = f"https://gmail.googleapis.com/gmail/v1/users/me/threads/{safe_id}?format=metadata&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Message-ID&metadataHeaders=In-Reply-To"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def load_verified_receipts(directory: Path) -> list[dict[str, Any]]:
    receipts = []
    for path in sorted(directory.glob("*_gmail_api_receipt.json")):
        row = json.loads(path.read_text(encoding="utf-8"))
        if row.get("status") == "verified" and row.get("thread_id"):
            row["receipt_path"] = str(path)
            receipts.append(row)
    return receipts


def sync(*, token_path: Path, receipts_dir: Path) -> dict[str, Any]:
    access, account = refresh_access_token(token_path)
    rows = []
    for receipt in load_verified_receipts(receipts_dir):
        outcome = classify_thread(gmail_thread(receipt["thread_id"], access), own_email=account)
        rows.append({
            "thread_id": receipt["thread_id"], "gmail_message_id": receipt.get("gmail_message_id"),
            "to": receipt.get("to"), "subject": receipt.get("subject"),
            "receipt_path": receipt["receipt_path"], **outcome,
        })
    return {"account": account, "summary": summarize(rows), "messages": rows}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", type=Path, default=DEFAULT_TOKEN)
    parser.add_argument("--receipts", type=Path, default=DEFAULT_RECEIPTS)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()
    report = sync(token_path=args.token, receipts_dir=args.receipts)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
