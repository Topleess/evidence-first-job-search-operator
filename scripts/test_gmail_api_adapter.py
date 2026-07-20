from __future__ import annotations
import base64
from email.message import EmailMessage
from gmail_api_adapter import GmailApiClient, GmailApiReplyStore, GmailApiSentStore, GmailApiTransport


class Call:
    def __init__(self, value): self.value = value
    def execute(self): return self.value


class Messages:
    def __init__(self): self.sent_body = None; self.query = None
    def send(self, *, userId, body): self.sent_body = body; return Call({"id": "provider-send-1"})
    def list(self, *, userId, q, maxResults): self.query = q; return Call({"messages": [{"id": "provider-send-1"}]})
    def get(self, **kwargs):
        return Call({"threadId":"thread-1","payload": {"headers": [
            {"name": "Message-ID", "value": "<job-1@example.invalid>"},
            {"name": "Date", "value": "Sun, 19 Jul 2026 19:10:00 +0000"},
        ]}})

class Threads:
    def get(self, **kwargs):
        return Call({"messages":[
            {"id":"provider-send-1","labelIds":["SENT"],"payload":{"headers":[]}},
            {"id":"provider-reply-1","labelIds":["INBOX"],"payload":{"headers":[
                {"name":"In-Reply-To","value":"<job-1@example.invalid>"},
                {"name":"Date","value":"Sun, 19 Jul 2026 20:10:00 +0000"},
            ]}},
        ]})


class Service:
    def __init__(self): self.messages_api = Messages(); self.threads_api = Threads()
    def users(self): return self
    def messages(self): return self.messages_api
    def threads(self): return self.threads_api


def test_oauth_api_transport_and_exact_sent_readback(tmp_path):
    service = Service()
    client = GmailApiClient(token_file=tmp_path / "unused", service_factory=lambda: service)
    message = EmailMessage(); message["From"] = "candidate@example.com"; message["To"] = "hr@example.org"
    message["Message-ID"] = "<job-1@example.invalid>"; message["Subject"] = "Vacancy"; message.set_content("Hello")
    GmailApiTransport(client=client).send(message.as_bytes())
    assert service.messages_api.sent_body is not None
    decoded = base64.urlsafe_b64decode(service.messages_api.sent_body["raw"] + "==")
    assert b"Message-ID: <job-1@example.invalid>" in decoded
    found = GmailApiSentStore(client=client).find_exact_message_id("<job-1@example.invalid>")
    assert service.messages_api.query == "in:sent rfc822msgid:<job-1@example.invalid>"
    assert found == {"uid": "provider-send-1", "sent_at": "2026-07-19T19:10:00+00:00", "evidence_ref": "gmail-api://users/me/messages/provider-send-1"}
    reply = GmailApiReplyStore(client=client).find_reply_to("<job-1@example.invalid>")
    assert reply == {"uid":"provider-reply-1","responded_at":"2026-07-19T20:10:00+00:00","evidence_ref":"gmail-api://users/me/messages/provider-reply-1"}
