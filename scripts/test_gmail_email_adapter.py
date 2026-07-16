from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_adapter():
    path = ROOT / "scripts" / "gmail_email_adapter.py"
    spec = importlib.util.spec_from_file_location("gmail_email_adapter_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_modified_utf7_encodes_localized_gmail_sent_folder():
    adapter = load_adapter()
    assert adapter.imap_modified_utf7("[Gmail]/Отправленные") == (
        "[Gmail]/&BB4EQgQ,BEAEMAQyBDsENQQ9BD0ESwQ1-"
    )
    assert adapter.imap_modified_utf7("A&B") == "A&-B"


class FakeImap:
    def __init__(self, *_args, **_kwargs): self.calls = []
    def __enter__(self): return self
    def __exit__(self, *_args): return None
    def login(self, username, password):
        self.calls.append(("login", username, password)); return "OK", []
    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly)); return "OK", []
    def uid(self, command, *args):
        self.calls.append(("uid", command, *args))
        if command == "search": return "OK", [b"77"]
        return "OK", [(b"77", b"Message-ID: <reply@example.org>\r\nIn-Reply-To: <initial@example.com>\r\nDate: Sat, 18 Jul 2026 09:00:00 +0000\r\n\r\n")]


def test_reply_store_requires_exact_in_reply_to_and_uses_readonly_inbox(tmp_path):
    adapter = load_adapter()
    password = tmp_path / "credential"
    password.write_text("not-a-real-secret", encoding="utf-8")
    fake = FakeImap()
    store = adapter.GmailImapReplyStore(username="candidate@example.com", password_file=password, imap_factory=lambda *_a, **_kw: fake)
    found = store.find_reply_to("<initial@example.com>")
    assert found == {"uid": "77", "responded_at": "2026-07-18T09:00:00+00:00", "evidence_ref": "imap://imap.gmail.com/INBOX/uid/77"}
    assert ("select", "INBOX", True) in fake.calls
    assert ("uid", "search", None, "HEADER", "In-Reply-To", "<initial@example.com>") in fake.calls
