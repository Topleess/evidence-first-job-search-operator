#!/usr/bin/env python3
"""Gmail OAuth API transport and exact Sent read-back adapters.

Uses the existing user-authorized OAuth token. Secrets remain outside the
job-search workspace; this module stores only provider message IDs as evidence.
"""
from __future__ import annotations

import base64
import email
from email import policy
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable


class GmailApiClient:
    def __init__(self, *, token_file: str | Path, service_factory: Callable[..., Any] | None = None) -> None:
        self.token_file = Path(token_file)
        self.service_factory = service_factory
        self._service = None

    def service(self):
        if self._service is not None:
            return self._service
        if self.service_factory is not None:
            self._service = self.service_factory()
            return self._service
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        creds = Credentials.from_authorized_user_file(str(self.token_file))
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            self.token_file.write_text(creds.to_json(), encoding="utf-8")
        if not creds.valid:
            raise RuntimeError("Gmail OAuth credentials are invalid")
        self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service


class GmailApiTransport:
    def __init__(self, *, client: GmailApiClient) -> None:
        self.client = client

    def send(self, raw_message: bytes) -> None:
        message = email.message_from_bytes(raw_message, policy=policy.default)
        if not message.get("To") or not message.get("Message-ID"):
            raise ValueError("MIME recipient and Message-ID are required")
        raw = base64.urlsafe_b64encode(raw_message).decode("ascii").rstrip("=")
        result = self.client.service().users().messages().send(userId="me", body={"raw": raw}).execute()
        if not result.get("id"):
            raise RuntimeError("Gmail API send returned no provider message ID")


class GmailApiSentStore:
    def __init__(self, *, client: GmailApiClient) -> None:
        self.client = client

    def find_exact_message_id(self, message_id: str) -> dict[str, Any] | None:
        target = str(message_id).strip()
        if not target.startswith("<") or not target.endswith(">") or len(target) > 998:
            raise ValueError("invalid Message-ID")
        service = self.client.service()
        result = service.users().messages().list(userId="me", q=f"in:sent rfc822msgid:{target}", maxResults=10).execute()
        for item in result.get("messages", []):
            provider_id = str(item.get("id", ""))
            if not provider_id:
                continue
            found = service.users().messages().get(
                userId="me", id=provider_id, format="metadata",
                metadataHeaders=["Message-ID", "Date"],
            ).execute()
            headers = {h.get("name", "").lower(): h.get("value", "") for h in found.get("payload", {}).get("headers", [])}
            if headers.get("message-id", "").strip() != target:
                continue
            date_value = headers.get("date", "").strip()
            if not date_value:
                raise RuntimeError("Gmail Sent message lacks Date header")
            return {
                "uid": provider_id,
                "sent_at": parsedate_to_datetime(date_value).isoformat(),
                "evidence_ref": f"gmail-api://users/me/messages/{provider_id}",
            }
        return None


class GmailApiReplyStore:
    """Independent thread read-back for replies to a deterministic Message-ID."""
    def __init__(self, *, client: GmailApiClient) -> None:
        self.client = client

    def find_reply_to(self, message_id: str) -> dict[str, Any] | None:
        target = str(message_id).strip()
        if not target.startswith("<") or not target.endswith(">") or len(target) > 998:
            raise ValueError("invalid Message-ID")
        service = self.client.service()
        listed = service.users().messages().list(userId="me", q=f"in:sent rfc822msgid:{target}", maxResults=10).execute()
        for item in listed.get("messages", []):
            provider_id = str(item.get("id") or "")
            if not provider_id:
                continue
            sent = service.users().messages().get(userId="me", id=provider_id, format="metadata", metadataHeaders=["Message-ID"]).execute()
            headers = {h.get("name", "").lower(): h.get("value", "") for h in sent.get("payload", {}).get("headers", [])}
            if headers.get("message-id", "").strip() != target:
                continue
            thread_id = str(sent.get("threadId") or "")
            if not thread_id:
                continue
            thread = service.users().threads().get(userId="me", id=thread_id, format="metadata", metadataHeaders=["Message-ID", "In-Reply-To", "References", "Date"]).execute()
            for msg in thread.get("messages", []):
                if str(msg.get("id") or "") == provider_id or "SENT" in (msg.get("labelIds") or []):
                    continue
                reply_headers = {h.get("name", "").lower(): h.get("value", "") for h in msg.get("payload", {}).get("headers", [])}
                related = target in reply_headers.get("in-reply-to", "") or target in reply_headers.get("references", "")
                if not related:
                    continue
                date_value = reply_headers.get("date", "").strip()
                if not date_value:
                    raise RuntimeError("Gmail reply lacks Date header")
                reply_id = str(msg.get("id") or "")
                return {"uid": reply_id, "responded_at": parsedate_to_datetime(date_value).isoformat(), "evidence_ref": f"gmail-api://users/me/messages/{reply_id}"}
        return None
