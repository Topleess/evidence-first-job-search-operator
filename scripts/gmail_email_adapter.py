#!/usr/bin/env python3
"""Gmail SMTP transport and read-only IMAP Sent reconciliation adapters."""
from __future__ import annotations

import base64
import email
import imaplib
import smtplib
import ssl
from email import policy
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable


def imap_modified_utf7(value: str) -> str:
    """Encode a mailbox name using RFC 3501 modified UTF-7."""
    output: list[str] = []
    non_ascii: list[str] = []

    def flush() -> None:
        if not non_ascii:
            return
        raw = "".join(non_ascii).encode("utf-16-be")
        encoded = base64.b64encode(raw).decode("ascii").rstrip("=").replace("/", ",")
        output.append(f"&{encoded}-")
        non_ascii.clear()

    for char in value:
        code = ord(char)
        if 0x20 <= code <= 0x7E:
            flush()
            output.append("&-" if char == "&" else char)
        else:
            non_ascii.append(char)
    flush()
    return "".join(output)


class GmailSmtpTransport:
    def __init__(
        self,
        *,
        username: str,
        password_file: str | Path,
        host: str = "smtp.gmail.com",
        port: int = 587,
        timeout: float = 30.0,
        smtp_factory: Callable[..., Any] = smtplib.SMTP,
    ) -> None:
        self.username = username
        self.password_file = Path(password_file)
        self.host = host
        self.port = port
        self.timeout = timeout
        self.smtp_factory = smtp_factory

    def _password(self) -> str:
        password = self.password_file.read_text(encoding="utf-8").strip()
        if not password:
            raise RuntimeError("empty Gmail credential")
        return password

    def send(self, raw_message: bytes) -> None:
        message = email.message_from_bytes(raw_message, policy=policy.default)
        sender = str(message.get("From", "")).strip()
        recipient = str(message.get("To", "")).strip()
        message_id = str(message.get("Message-ID", "")).strip()
        if sender.lower() != self.username.lower():
            raise ValueError("MIME sender does not match configured Gmail account")
        if not recipient or not message_id:
            raise ValueError("MIME recipient and Message-ID are required")
        with self.smtp_factory(self.host, self.port, timeout=self.timeout) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(self.username, self._password())
            refused = smtp.sendmail(sender, [recipient], raw_message)
            if refused:
                raise RuntimeError("SMTP refused one or more recipients")


class GmailImapSentStore:
    def __init__(
        self,
        *,
        username: str,
        password_file: str | Path,
        sent_folder: str = "[Gmail]/Отправленные",
        host: str = "imap.gmail.com",
        port: int = 993,
        timeout: float = 30.0,
        imap_factory: Callable[..., Any] = imaplib.IMAP4_SSL,
    ) -> None:
        self.username = username
        self.password_file = Path(password_file)
        self.sent_folder = sent_folder
        self.host = host
        self.port = port
        self.timeout = timeout
        self.imap_factory = imap_factory

    def _password(self) -> str:
        password = self.password_file.read_text(encoding="utf-8").strip()
        if not password:
            raise RuntimeError("empty Gmail credential")
        return password

    def find_exact_message_id(self, message_id: str) -> dict[str, Any] | None:
        target = str(message_id).strip()
        if not target.startswith("<") or not target.endswith(">") or len(target) > 998:
            raise ValueError("invalid Message-ID")
        with self.imap_factory(self.host, self.port, timeout=self.timeout) as imap:
            status, _ = imap.login(self.username, self._password())
            if status != "OK":
                raise RuntimeError("IMAP login failed")
            status, _ = imap.select(imap_modified_utf7(self.sent_folder), readonly=True)
            if status != "OK":
                raise RuntimeError("Gmail Sent folder unavailable")
            status, data = imap.uid("search", None, "HEADER", "Message-ID", target)
            if status != "OK":
                raise RuntimeError("IMAP Message-ID search failed")
            uids = data[0].split() if data and data[0] else []
            for raw_uid in reversed(uids):
                uid = raw_uid.decode("ascii")
                status, fetched = imap.uid(
                    "fetch", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID DATE)])"
                )
                if status != "OK":
                    continue
                header_bytes = next(
                    (part[1] for part in fetched if isinstance(part, tuple) and len(part) > 1),
                    None,
                )
                if not header_bytes:
                    continue
                header = email.message_from_bytes(header_bytes, policy=policy.default)
                observed = str(header.get("Message-ID", "")).strip()
                if observed != target:
                    continue
                date_value = str(header.get("Date", "")).strip()
                if not date_value:
                    raise RuntimeError("Sent message lacks Date header")
                sent_at = parsedate_to_datetime(date_value).isoformat()
                return {
                    "uid": uid,
                    "sent_at": sent_at,
                    "evidence_ref": f"imap://{self.host}/{self.sent_folder}/uid/{uid}",
                }
            return None


class GmailImapReplyStore:
    """Read-only exact-thread reply lookup in Gmail INBOX."""
    def __init__(self, *, username: str, password_file: str | Path,
                 inbox_folder: str = "INBOX", host: str = "imap.gmail.com",
                 port: int = 993, timeout: float = 30.0,
                 imap_factory: Callable[..., Any] = imaplib.IMAP4_SSL) -> None:
        self.username = username
        self.password_file = Path(password_file)
        self.inbox_folder = inbox_folder
        self.host = host
        self.port = port
        self.timeout = timeout
        self.imap_factory = imap_factory

    def _password(self) -> str:
        password = self.password_file.read_text(encoding="utf-8").strip()
        if not password:
            raise RuntimeError("empty Gmail credential")
        return password

    def find_reply_to(self, message_id: str) -> dict[str, Any] | None:
        target = str(message_id).strip()
        if not target.startswith("<") or not target.endswith(">") or len(target) > 998:
            raise ValueError("invalid Message-ID")
        with self.imap_factory(self.host, self.port, timeout=self.timeout) as imap:
            status, _ = imap.login(self.username, self._password())
            if status != "OK": raise RuntimeError("IMAP login failed")
            status, _ = imap.select(imap_modified_utf7(self.inbox_folder), readonly=True)
            if status != "OK": raise RuntimeError("Gmail INBOX unavailable")
            status, data = imap.uid("search", None, "HEADER", "In-Reply-To", target)
            if status != "OK": raise RuntimeError("IMAP In-Reply-To search failed")
            uids = data[0].split() if data and data[0] else []
            for raw_uid in reversed(uids):
                uid = raw_uid.decode("ascii")
                status, fetched = imap.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (MESSAGE-ID IN-REPLY-TO DATE)])")
                if status != "OK": continue
                raw = next((part[1] for part in fetched if isinstance(part, tuple) and len(part) > 1), None)
                if not raw: continue
                header = email.message_from_bytes(raw, policy=policy.default)
                if str(header.get("In-Reply-To", "")).strip() != target: continue
                response_id = str(header.get("Message-ID", "")).strip()
                date_value = str(header.get("Date", "")).strip()
                if not response_id or not date_value: continue
                return {"uid": uid, "responded_at": parsedate_to_datetime(date_value).isoformat(),
                        "evidence_ref": f"imap://{self.host}/{self.inbox_folder}/uid/{uid}"}
            return None
