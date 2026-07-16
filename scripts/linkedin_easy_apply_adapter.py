#!/usr/bin/env python3
"""LinkedIn Easy Apply form classification and fenced submission state.

Browser actions live in a separate worker. This module owns deterministic field
classification, durable intent fencing, ambiguous-submit handling, and verified receipts.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit


class UnsafeAction(RuntimeError):
    pass


class IntentFenceViolation(RuntimeError):
    pass


class AuthState(str, Enum):
    AUTHENTICATED = "authenticated"
    LOGIN_REQUIRED = "login_required"
    CHALLENGE = "challenge"
    UNKNOWN = "unknown"


def classify_auth(url: str, body_text: str) -> AuthState:
    url_lower = url.lower()
    text = body_text[:5000].lower()
    if any(marker in url_lower for marker in ("/checkpoint/", "/challenge/")) or any(
        marker in text for marker in ("security verification", "verify your identity", "проверка безопасности")
    ):
        return AuthState.CHALLENGE
    if "/login" in url_lower or re.search(r"\bsign in\b|войти в linkedin", text):
        return AuthState.LOGIN_REQUIRED
    if "/jobs/" in url_lower and any(marker in text for marker in ("easy apply", "jobs", "ваканси", "простая подача")):
        return AuthState.AUTHENTICATED
    return AuthState.UNKNOWN


@dataclass(frozen=True)
class KnownProfile:
    first_name: str
    last_name: str
    email: str
    phone: str


@dataclass(frozen=True)
class FieldSpec:
    key: str
    label: str
    kind: str
    required: bool
    value: str = ""
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class Classification:
    fills: dict[str, str]
    blockers: list[FieldSpec]
    untouched_optional: list[str]

    @property
    def ready_for_review(self) -> bool:
        return not self.blockers

    def fingerprint(self, fields: Iterable[FieldSpec]) -> str:
        canonical = json.dumps([asdict(field) for field in fields], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


class FormClassifier:
    _KNOWN_LABELS = {
        "first_name": (r"\bfirst\s*name\b", r"^имя$"),
        "last_name": (r"\blast\s*name\b", r"фамили"),
        "email": (r"e-?mail", r"электронн.*почт"),
        "phone": (r"(?:mobile\s*)?phone(?:\s*number)?", r"номер.*телефон"),
    }

    def __init__(self, profile: KnownProfile, answer_map: dict[str, str] | None = None) -> None:
        self.profile = profile
        self.answer_map = dict(answer_map or {})

    def _known_value(self, field: FieldSpec) -> str | None:
        if field.key in self.answer_map:
            value = str(self.answer_map[field.key]).strip()
            if field.options and value not in field.options:
                return None
            return value or None
        label = field.label.strip().lower()
        for attr, patterns in self._KNOWN_LABELS.items():
            if any(re.search(pattern, label, re.I) for pattern in patterns):
                return str(getattr(self.profile, attr))
        return None

    def classify(self, fields: Iterable[FieldSpec]) -> Classification:
        fills: dict[str, str] = {}
        blockers: list[FieldSpec] = []
        optional: list[str] = []
        for field in fields:
            if field.value.strip():
                continue
            known = self._known_value(field)
            if known:
                fills[field.key] = known
            elif field.required:
                blockers.append(field)
            else:
                optional.append(field.key)
        return Classification(fills=fills, blockers=blockers, untouched_optional=optional)


@dataclass(frozen=True)
class IntentReservation:
    intent_id: int
    created: bool


class DurableIntentStore:
    """SQLite ledger scoped to dry-run preparation only."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.path, timeout=10)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA busy_timeout=10000")
        self.con.execute(
            """CREATE TABLE IF NOT EXISTS linkedin_dry_run_intents (
                id INTEGER PRIMARY KEY,
                job_id TEXT NOT NULL,
                job_url TEXT NOT NULL,
                form_fingerprint TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('prepared','blocked_required','blocked_stale','submitting','ambiguous','verified')),
                evidence_json TEXT,
                executing_by TEXT,
                execution_token TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(job_id, form_fingerprint)
            )"""
        )
        self.con.execute(
            """CREATE TABLE IF NOT EXISTS linkedin_submission_receipts (
                id INTEGER PRIMARY KEY,
                intent_id INTEGER NOT NULL UNIQUE REFERENCES linkedin_dry_run_intents(id),
                job_id TEXT NOT NULL UNIQUE,
                payload_digest TEXT NOT NULL,
                evidence_sha256 TEXT NOT NULL,
                marker TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        self.con.commit()

    def close(self) -> None:
        self.con.close()

    @staticmethod
    def _validate_job(job_id: str, job_url: str) -> tuple[str, str]:
        jid = str(job_id).strip()
        parsed = urlsplit(job_url)
        match = re.fullmatch(r"/jobs/view/(?:[^/]*-)?(\d+)/?", parsed.path)
        if parsed.scheme != "https" or parsed.hostname not in {"linkedin.com", "www.linkedin.com"} or not match:
            raise ValueError("job_url must be a canonical LinkedIn job URL")
        if jid != match.group(1):
            raise ValueError("job_id does not match job_url")
        return jid, f"https://www.linkedin.com/jobs/view/{jid}/"

    def reserve(self, *, job_id: str, job_url: str, form_fingerprint: str, payload: dict[str, Any]) -> IntentReservation:
        jid, canonical_url = self._validate_job(job_id, job_url)
        fingerprint = str(form_fingerprint).strip()
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", fingerprint):
            raise ValueError("invalid form fingerprint")
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(payload_json.encode()) > 65536:
            raise ValueError("payload too large")
        self.con.execute("BEGIN IMMEDIATE")
        try:
            existing = self.con.execute(
                "SELECT id,payload_json FROM linkedin_dry_run_intents WHERE job_id=? AND form_fingerprint=?",
                (jid, fingerprint),
            ).fetchone()
            if existing:
                if existing["payload_json"] != payload_json:
                    raise ValueError("payload conflict for durable intent")
                self.con.commit()
                return IntentReservation(int(existing["id"]), False)
            self.con.execute(
                "UPDATE linkedin_dry_run_intents SET state='blocked_stale',updated_at=CURRENT_TIMESTAMP WHERE job_id=? AND state!='blocked_stale'",
                (jid,),
            )
            cursor = self.con.execute(
                "INSERT INTO linkedin_dry_run_intents(job_id,job_url,form_fingerprint,payload_json,state) VALUES(?,?,?,?, 'prepared')",
                (jid, canonical_url, fingerprint, payload_json),
            )
            intent_id = cursor.lastrowid
            if intent_id is None:
                raise RuntimeError("intent insert returned no row id")
            self.con.commit()
            return IntentReservation(int(intent_id), True)
        except Exception:
            self.con.rollback()
            raise

    def record_dry_run(self, intent_id: int, *, evidence: dict[str, Any]) -> None:
        evidence_json = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        stop_reason = evidence.get("stop_reason")
        state = "blocked_required" if stop_reason == "unknown_required" else "prepared"
        updated = self.con.execute(
            "UPDATE linkedin_dry_run_intents SET state=?,evidence_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=? AND state!='blocked_stale'",
            (state, evidence_json, intent_id),
        )
        if updated.rowcount != 1:
            self.con.rollback()
            raise ValueError("unknown or stale durable intent")
        self.con.commit()

    def get(self, intent_id: int) -> dict[str, Any]:
        row = self.con.execute("SELECT * FROM linkedin_dry_run_intents WHERE id=?", (intent_id,)).fetchone()
        if row is None:
            raise ValueError("unknown durable intent")
        return dict(row)

    def begin_submit(self, intent_id: int, *, worker_id: str) -> str:
        worker = str(worker_id).strip()
        if not worker or len(worker) > 128:
            raise ValueError("invalid worker_id")
        token = secrets.token_urlsafe(32)
        self.con.execute("BEGIN IMMEDIATE")
        try:
            updated = self.con.execute(
                """UPDATE linkedin_dry_run_intents
                   SET state='submitting',executing_by=?,execution_token=?,updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND state='prepared'""",
                (worker, token, intent_id),
            )
            if updated.rowcount != 1:
                raise IntentFenceViolation("intent is not prepared for submit")
            self.con.commit()
            return token
        except Exception:
            self.con.rollback()
            raise

    def mark_submit_ambiguous(self, intent_id: int, *, execution_token: str, reason: str) -> None:
        detail = str(reason).strip()
        evidence = json.dumps({"reason": detail}, sort_keys=True, separators=(",", ":"))
        updated = self.con.execute(
            """UPDATE linkedin_dry_run_intents
               SET state='ambiguous',evidence_json=?,executing_by=NULL,execution_token=NULL,updated_at=CURRENT_TIMESTAMP
               WHERE id=? AND state='submitting' AND execution_token=?""",
            (evidence, intent_id, execution_token),
        )
        if updated.rowcount != 1:
            self.con.rollback()
            raise IntentFenceViolation("stale execution token")
        self.con.commit()

    def record_submit_readback(
        self,
        intent_id: int,
        *,
        execution_token: str,
        readback: dict[str, Any],
        evidence_bytes: bytes,
    ) -> int:
        marker = str(readback.get("marker") or "")
        observed_job_id = str(readback.get("job_id") or "")
        if marker not in {"application_submitted", "applied_state_on_job_page"}:
            raise ValueError("verified submission marker is required")
        if not evidence_bytes:
            raise ValueError("read-back evidence is required")
        self.con.execute("BEGIN IMMEDIATE")
        try:
            row = self.con.execute(
                "SELECT job_id,payload_json,state,execution_token FROM linkedin_dry_run_intents WHERE id=?",
                (intent_id,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown durable intent")
            if observed_job_id != row["job_id"]:
                raise ValueError("read-back job identity does not match intent")
            existing = self.con.execute(
                "SELECT id,evidence_sha256,marker FROM linkedin_submission_receipts WHERE intent_id=?",
                (intent_id,),
            ).fetchone()
            evidence_sha256 = hashlib.sha256(evidence_bytes).hexdigest()
            if existing is not None:
                if existing["evidence_sha256"] != evidence_sha256 or existing["marker"] != marker:
                    raise ValueError("receipt evidence conflict")
                self.con.commit()
                return int(existing["id"])
            if row["state"] != "submitting" or row["execution_token"] != execution_token:
                raise IntentFenceViolation("stale execution token")
            payload_digest = hashlib.sha256(row["payload_json"].encode()).hexdigest()
            cursor = self.con.execute(
                """INSERT INTO linkedin_submission_receipts
                   (intent_id,job_id,payload_digest,evidence_sha256,marker)
                   VALUES(?,?,?,?,?)""",
                (intent_id, row["job_id"], payload_digest, evidence_sha256, marker),
            )
            receipt_id = cursor.lastrowid
            if receipt_id is None:
                raise RuntimeError("receipt insert returned no row id")
            updated = self.con.execute(
                """UPDATE linkedin_dry_run_intents
                   SET state='verified',evidence_json=?,executing_by=NULL,execution_token=NULL,updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND state='submitting' AND execution_token=?""",
                (json.dumps(readback, sort_keys=True, separators=(",", ":")), intent_id, execution_token),
            )
            if updated.rowcount != 1:
                raise IntentFenceViolation("stale execution token")
            self.con.commit()
            return int(receipt_id)
        except Exception:
            self.con.rollback()
            raise

    def list_receipts(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.con.execute("SELECT * FROM linkedin_submission_receipts ORDER BY id")]

    def mark_submitted(self, intent_id: int) -> None:
        del intent_id
        raise UnsafeAction("final Submit is disabled without fenced browser execution and verified read-back")
