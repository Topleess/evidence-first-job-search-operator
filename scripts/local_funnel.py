#!/usr/bin/env python3
"""Local SQLite control plane for the private job-search funnel.

Google Sheets is intentionally not on the critical path. Collection records are
stored with job-funnel-core, while private application receipts and follow-up
state live in companion tables in the same SQLite database.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "state" / "job_funnel.sqlite3"
CORE_SRC = Path("/opt/data/job-funnel-public/src")
if not CORE_SRC.exists():
    raise RuntimeError(f"job-funnel-core is missing: {CORE_SRC}")
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from job_funnel import Job, SQLiteStore  # noqa: E402
from job_funnel.storage import IdempotencyConflict  # noqa: E402

UTC = timezone.utc
HIGH_FIT_THRESHOLD = 70
TERMINAL_APPLICATION_STATUSES = {"rejected", "withdrawn", "offer", "hired", "closed"}
FOLLOWUP_ELIGIBLE_STATUSES = {"submitted", "applied", "seen", "viewed", "screening"}
SOURCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")


class ActionIntentConflict(RuntimeError):
    """An idempotency key was reused for different external work."""


class BatchQuotaExceeded(RuntimeError):
    """A batch attempted to reserve more external actions than authorized."""


class IntentFenceViolation(RuntimeError):
    """An intent transition used a stale token or an unsafe state."""


class WorkflowPaused(RuntimeError):
    """External side effects are disabled by the durable emergency pause."""


@dataclass(frozen=True)
class ActionIntentReservation:
    intent_id: int
    created: bool


@dataclass(frozen=True)
class ReconciliationClaim:
    intent_id: int
    payload: dict[str, Any]
    reconciliation_token: str
    reconciliation_attempt: int


@dataclass(frozen=True)
class EmailIntentReservation:
    intent_id: int
    created: bool
    message_id: str


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_time(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return result.astimezone(UTC)


def source_name(value: object) -> str:
    candidate = str(value or "unknown").strip().lower().replace(" ", "-")
    candidate = {"hh.ru": "hh", "www.hh.ru": "hh", "linkedin.com": "linkedin", "www.linkedin.com": "linkedin"}.get(candidate, candidate)
    candidate = re.sub(r"[^a-z0-9_.-]+", "-", candidate).strip("-.")
    if not SOURCE_RE.fullmatch(candidate):
        raise ValueError("invalid source")
    return candidate


def canonical_url(value: object) -> str:
    url = str(value or "").strip()
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("missing absolute job URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("job URL must not contain credentials")
    return url


def external_id_for(source: str, url: str, explicit: object = "") -> str:
    provided = str(explicit or "").strip()
    if provided:
        return provided[:256]
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    if source == "linkedin" and host in {"linkedin.com", "www.linkedin.com"}:
        match = re.search(r"/jobs/view/(?:[^/]*-)?(\d+)$", path)
        if match:
            return match.group(1)
    if source == "hh" and (host == "hh.ru" or host.endswith(".hh.ru")):
        match = re.search(r"/vacancy/(\d+)$", path)
        if match:
            return match.group(1)
        vacancy_id = parse_qs(parsed.query).get("vacancyId", [""])[0]
        if vacancy_id.isdigit():
            return vacancy_id
    if source == "telegram" and host in {"t.me", "telegram.me"}:
        parts = [part for part in path.split("/") if part]
        if len(parts) >= 2:
            return "/".join(parts[-2:])[:256]
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return f"url-{digest}"


def score_of(row: dict[str, Any]) -> int:
    try:
        return max(0, min(100, int(float(str(row.get("fit_score", 0)).strip() or 0))))
    except (TypeError, ValueError):
        return 0


def rows_from_payload(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if isinstance(payload, dict):
        for key in ("rows", "jobs", "vacancies", "results", "items"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    raise ValueError("JSON must be a list of rows or an object containing a row list")


class LocalFunnel:
    def __init__(self, path: str | Path = DEFAULT_DB) -> None:
        self.path = Path(path)
        self.store = SQLiteStore(self.path)
        self._migrate_private_tables()

    def __enter__(self) -> "LocalFunnel":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.store.close()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=10000")
        return con

    def _migrate_private_tables(self) -> None:
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                """CREATE TABLE IF NOT EXISTS application_receipts (
                    id INTEGER PRIMARY KEY,
                    job_id INTEGER REFERENCES jobs(id),
                    job_url TEXT NOT NULL,
                    external_vacancy_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    company TEXT NOT NULL,
                    job_title TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT 'platform',
                    status TEXT NOT NULL,
                    submitted INTEGER NOT NULL DEFAULT 0 CHECK(submitted IN (0,1)),
                    read_back_verified INTEGER NOT NULL DEFAULT 0 CHECK(read_back_verified IN (0,1)),
                    submitted_at TEXT,
                    response_at TEXT,
                    evidence_path TEXT,
                    updated_at TEXT NOT NULL,
                    UNIQUE(source, external_vacancy_id)
                )"""
            )
            con.execute(
                """CREATE TABLE IF NOT EXISTS status_events (
                    id INTEGER PRIMARY KEY,
                    receipt_id INTEGER NOT NULL REFERENCES application_receipts(id),
                    at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    UNIQUE(receipt_id, at, status)
                )"""
            )
            con.execute(
                """CREATE TABLE IF NOT EXISTS source_imports (
                    id INTEGER PRIMARY KEY,
                    path TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    accepted INTEGER NOT NULL,
                    rejected INTEGER NOT NULL
                )"""
            )
            con.execute(
                """CREATE TABLE IF NOT EXISTS batch_runs (
                    id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('running','completed','failed','paused')),
                    max_actions INTEGER NOT NULL CHECK(max_actions > 0),
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    detail TEXT NOT NULL DEFAULT '',
                    heartbeat_at TEXT,
                    stop_reason TEXT
                )"""
            )
            batch_columns = {row[1] for row in con.execute("PRAGMA table_info(batch_runs)")}
            for column, declaration in (
                ("heartbeat_at", "TEXT"),
                ("stop_reason", "TEXT"),
            ):
                if column not in batch_columns:
                    con.execute(f"ALTER TABLE batch_runs ADD COLUMN {column} {declaration}")
            con.execute(
                """CREATE TABLE IF NOT EXISTS workflow_control (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    paused INTEGER NOT NULL CHECK(paused IN (0,1)),
                    reason TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            con.execute(
                """INSERT OR IGNORE INTO workflow_control(singleton,paused,reason,updated_at)
                VALUES(1,0,'',?)""",
                (utc_now().isoformat(),),
            )
            con.execute(
                """CREATE TABLE IF NOT EXISTS action_intents (
                    id INTEGER PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES batch_runs(id),
                    kind TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'reserved'
                      CHECK(state IN ('reserved','executing','ambiguous','verified','failed','blocked')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(kind,idempotency_key)
                )"""
            )
            intent_columns = {
                row[1] for row in con.execute("PRAGMA table_info(action_intents)")
            }
            for column, declaration in (
                ("executing_by", "TEXT"),
                ("execution_token", "TEXT"),
                ("execution_started_at", "TEXT"),
                ("side_effect_maybe_at", "TEXT"),
                ("last_error_code", "TEXT"),
                ("reconciling_by", "TEXT"),
                ("reconciliation_token", "TEXT"),
                ("reconciliation_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("next_reconcile_at", "TEXT"),
            ):
                if column not in intent_columns:
                    con.execute(f"ALTER TABLE action_intents ADD COLUMN {column} {declaration}")
            receipt_columns = {
                row[1] for row in con.execute("PRAGMA table_info(application_receipts)")
            }
            if "run_id" not in receipt_columns:
                con.execute("ALTER TABLE application_receipts ADD COLUMN run_id TEXT")
            if "action_intent_id" not in receipt_columns:
                con.execute("ALTER TABLE application_receipts ADD COLUMN action_intent_id INTEGER")
            con.execute(
                """CREATE TABLE IF NOT EXISTS email_receipts (
                    id INTEGER PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES batch_runs(id),
                    action_intent_id INTEGER NOT NULL UNIQUE REFERENCES action_intents(id),
                    message_id TEXT NOT NULL UNIQUE,
                    sent_uid TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    evidence_ref TEXT NOT NULL,
                    read_back_verified INTEGER NOT NULL CHECK(read_back_verified IN (0,1)),
                    created_at TEXT NOT NULL
                )"""
            )
            con.execute(
                """CREATE TABLE IF NOT EXISTS email_followups (
                    id INTEGER PRIMARY KEY,
                    initial_intent_id INTEGER NOT NULL UNIQUE REFERENCES action_intents(id),
                    due_at TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','cancelled','reserved','verified')),
                    response_uid TEXT,
                    responded_at TEXT,
                    followup_intent_id INTEGER UNIQUE REFERENCES action_intents(id),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK((state='cancelled') = (response_uid IS NOT NULL AND responded_at IS NOT NULL))
                )"""
            )
            con.commit()

    def set_emergency_pause(
        self, *, paused: bool, reason: str, now: str | datetime
    ) -> None:
        if not isinstance(paused, bool):
            raise ValueError("paused must be boolean")
        detail = str(reason).strip()
        if not detail or len(detail) > 1000:
            raise ValueError("pause reason is required and limited to 1000 characters")
        timestamp = parse_time(now).isoformat()
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "UPDATE workflow_control SET paused=?,reason=?,updated_at=? WHERE singleton=1",
                (int(paused), detail, timestamp),
            )
            con.commit()

    def workflow_health(self, *, now: str | datetime) -> dict[str, Any]:
        timestamp = parse_time(now).isoformat()
        with closing(self._connect()) as con:
            control = con.execute(
                "SELECT paused,reason,updated_at FROM workflow_control WHERE singleton=1"
            ).fetchone()
            running = con.execute(
                "SELECT COUNT(*) FROM batch_runs WHERE state='running'"
            ).fetchone()[0]
            ambiguous = con.execute(
                "SELECT COUNT(*) FROM action_intents WHERE state='ambiguous'"
            ).fetchone()[0]
        return {
            "checked_at": timestamp,
            "paused": bool(control["paused"]),
            "pause_reason": control["reason"],
            "pause_updated_at": control["updated_at"],
            "running_batches": int(running),
            "ambiguous_intents": int(ambiguous),
        }

    def begin_batch_run(
        self, *, channel: str, max_actions: int, started_at: str | datetime
    ) -> str:
        channel = source_name(channel)
        if isinstance(max_actions, bool) or not isinstance(max_actions, int) or max_actions <= 0:
            raise ValueError("max_actions must be a positive integer")
        started = parse_time(started_at).isoformat()
        run_id = uuid.uuid4().hex
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            control = con.execute(
                "SELECT paused,reason FROM workflow_control WHERE singleton=1"
            ).fetchone()
            if control["paused"]:
                con.rollback()
                raise WorkflowPaused(control["reason"])
            con.execute(
                """INSERT INTO batch_runs
                (id,channel,state,max_actions,started_at,heartbeat_at)
                VALUES(?,?,'running',?,?,?)""",
                (run_id, channel, max_actions, started, started),
            )
            con.commit()
        return run_id

    def heartbeat_batch_run(self, *, run_id: str, now: str | datetime) -> None:
        timestamp = parse_time(now).isoformat()
        with closing(self._connect()) as con:
            updated = con.execute(
                "UPDATE batch_runs SET heartbeat_at=? WHERE id=? AND state='running'",
                (timestamp, run_id),
            )
            if updated.rowcount != 1:
                raise ValueError("batch run is not running")
            con.commit()

    def finish_batch_run(
        self, *, run_id: str, state: str, reason: str, now: str | datetime
    ) -> None:
        if state not in {"completed", "failed", "paused"}:
            raise ValueError("invalid terminal batch state")
        detail = str(reason).strip()
        if not detail or len(detail) > 1000:
            raise ValueError("finish reason is required and limited to 1000 characters")
        timestamp = parse_time(now).isoformat()
        with closing(self._connect()) as con:
            updated = con.execute(
                """UPDATE batch_runs
                SET state=?,finished_at=?,stop_reason=?,detail=?
                WHERE id=? AND state='running'""",
                (state, timestamp, detail, detail, run_id),
            )
            if updated.rowcount != 1:
                raise ValueError("batch run is not running")
            con.commit()

    def get_action_intent(self, *, intent_id: int) -> dict[str, Any]:
        with closing(self._connect()) as con:
            row = con.execute(
                """SELECT id,run_id,kind,idempotency_key,payload,state,created_at,updated_at
                FROM action_intents WHERE id=?""",
                (intent_id,),
            ).fetchone()
        if row is None:
            raise ValueError("unknown action intent")
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def prepare_email_intent(
        self,
        *,
        run_id: str,
        sender: str,
        recipient: str,
        recipient_verified: bool,
        recipient_provenance: str,
        vacancy_key: str,
        subject: str,
        body: str,
        now: str | datetime,
    ) -> EmailIntentReservation:
        sender_address = str(sender).strip().lower()
        recipient_address = str(recipient).strip().lower()
        if not EMAIL_RE.fullmatch(sender_address) or "\r" in sender_address or "\n" in sender_address:
            raise ValueError("invalid sender email")
        if not EMAIL_RE.fullmatch(recipient_address) or "\r" in recipient_address or "\n" in recipient_address:
            raise ValueError("invalid recipient email")
        if recipient_verified is not True:
            raise ValueError("recipient must be verified")
        provenance = str(recipient_provenance).strip()
        if not provenance or len(provenance) > 2000:
            raise ValueError("verified recipient provenance is required")
        vacancy = str(vacancy_key).strip()
        if not vacancy or len(vacancy) > 512:
            raise ValueError("invalid vacancy_key")
        mail_subject = str(subject).strip()
        if not mail_subject or len(mail_subject) > 998 or "\r" in mail_subject or "\n" in mail_subject:
            raise ValueError("invalid email subject")
        mail_body = str(body).strip()
        if not mail_body or len(mail_body.encode("utf-8")) > 100_000:
            raise ValueError("invalid email body")
        semantic = {
            "sender": sender_address,
            "recipient": recipient_address,
            "recipient_verified": True,
            "recipient_provenance": provenance,
            "vacancy_key": vacancy,
            "subject": mail_subject,
            "body": mail_body,
            "followup_number": 0,
        }
        canonical = json.dumps(
            semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        sender_domain = sender_address.rsplit("@", 1)[1]
        message_id = f"<job-search-{digest[:32]}@{sender_domain}>"
        payload = {**semantic, "message_id": message_id, "content_sha256": digest}
        semantic_key = hashlib.sha256(
            f"{recipient_address}\0{vacancy}".encode("utf-8")
        ).hexdigest()
        reserved = self.reserve_action_intent(
            run_id=run_id,
            kind="email_send",
            idempotency_key=f"email:{semantic_key}:initial",
            payload=payload,
            now=now,
        )
        return EmailIntentReservation(
            intent_id=reserved.intent_id,
            created=reserved.created,
            message_id=message_id,
        )

    def record_email_sent_readback(
        self,
        *,
        intent_id: int,
        message_id: str,
        sent_uid: str,
        sent_at: str | datetime,
        evidence_ref: str,
    ) -> int:
        exact_message_id = str(message_id).strip()
        uid = str(sent_uid).strip()
        evidence = str(evidence_ref).strip()
        if not exact_message_id or len(exact_message_id) > 998:
            raise ValueError("invalid Message-ID")
        if not uid or len(uid) > 512:
            raise ValueError("invalid Sent UID")
        if not evidence or len(evidence) > 4000:
            raise ValueError("Sent read-back evidence is required")
        sent_timestamp = parse_time(sent_at).isoformat()
        created_at = utc_now().isoformat()
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            intent = con.execute(
                "SELECT run_id,kind,payload,state FROM action_intents WHERE id=?",
                (intent_id,),
            ).fetchone()
            if intent is None or intent["kind"] not in {"email_send", "email_followup"}:
                con.rollback()
                raise ValueError("unknown email intent")
            payload = json.loads(intent["payload"])
            if payload.get("message_id") != exact_message_id:
                con.rollback()
                raise ValueError("Message-ID does not match durable email intent")
            if intent["state"] not in {"executing", "ambiguous", "verified"}:
                con.rollback()
                raise IntentFenceViolation("email was not executed before Sent read-back")
            existing = con.execute(
                "SELECT * FROM email_receipts WHERE action_intent_id=?",
                (intent_id,),
            ).fetchone()
            if existing is not None:
                expected = (
                    existing["message_id"],
                    existing["sent_uid"],
                    existing["sent_at"],
                    existing["evidence_ref"],
                )
                observed = (exact_message_id, uid, sent_timestamp, evidence)
                if expected != observed:
                    con.rollback()
                    raise ActionIntentConflict("email receipt evidence conflict")
                con.commit()
                return int(existing["id"])
            cursor = con.execute(
                """INSERT INTO email_receipts
                (run_id,action_intent_id,message_id,sent_uid,sent_at,evidence_ref,
                 read_back_verified,created_at)
                VALUES(?,?,?,?,?,?,1,?)""",
                (
                    intent["run_id"],
                    intent_id,
                    exact_message_id,
                    uid,
                    sent_timestamp,
                    evidence,
                    created_at,
                ),
            )
            receipt_id = cursor.lastrowid
            if receipt_id is None:
                con.rollback()
                raise RuntimeError("email receipt insert returned no row id")
            updated = con.execute(
                """UPDATE action_intents
                SET state='verified',updated_at=?,executing_by=NULL,execution_token=NULL,
                    reconciling_by=NULL,reconciliation_token=NULL,next_reconcile_at=NULL
                WHERE id=? AND state IN ('executing','ambiguous','verified')""",
                (sent_timestamp, intent_id),
            )
            if updated.rowcount != 1:
                con.rollback()
                raise IntentFenceViolation("email intent changed during Sent read-back")
            if intent["kind"] == "email_followup":
                followup_updated = con.execute(
                    """UPDATE email_followups SET state='verified',updated_at=?
                    WHERE followup_intent_id=? AND state='reserved'""",
                    (sent_timestamp, intent_id),
                )
                if followup_updated.rowcount != 1:
                    con.rollback()
                    raise IntentFenceViolation("follow-up ledger changed during Sent read-back")
            con.commit()
            return int(receipt_id)

    def schedule_email_followup(
        self,
        *,
        initial_intent_id: int,
        due_at: str | datetime,
        now: str | datetime,
    ) -> int:
        due_timestamp = parse_time(due_at).isoformat()
        now_timestamp = parse_time(now).isoformat()
        if parse_time(due_at) <= parse_time(now):
            raise ValueError("follow-up due_at must be in the future")
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            receipt = con.execute(
                """SELECT er.sent_at,ai.state,ai.kind
                FROM email_receipts er JOIN action_intents ai ON ai.id=er.action_intent_id
                WHERE er.action_intent_id=? AND er.read_back_verified=1""",
                (initial_intent_id,),
            ).fetchone()
            if receipt is None or receipt["kind"] != "email_send" or receipt["state"] != "verified":
                con.rollback()
                raise IntentFenceViolation("follow-up requires a verified initial email receipt")
            if parse_time(due_at) <= parse_time(receipt["sent_at"]):
                con.rollback()
                raise ValueError("follow-up must be due after initial send")
            existing = con.execute(
                "SELECT id,due_at FROM email_followups WHERE initial_intent_id=?",
                (initial_intent_id,),
            ).fetchone()
            if existing is not None:
                if existing["due_at"] != due_timestamp:
                    con.rollback()
                    raise ActionIntentConflict("follow-up schedule conflict")
                con.commit()
                return int(existing["id"])
            cursor = con.execute(
                """INSERT INTO email_followups
                (initial_intent_id,due_at,state,created_at,updated_at)
                VALUES(?,?,'pending',?,?)""",
                (initial_intent_id, due_timestamp, now_timestamp, now_timestamp),
            )
            followup_id = cursor.lastrowid
            if followup_id is None:
                con.rollback()
                raise RuntimeError("follow-up insert returned no row id")
            con.commit()
            return int(followup_id)

    def list_due_email_followups(
        self, *, now: str | datetime, limit: int = 20
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        timestamp = parse_time(now).isoformat()
        with closing(self._connect()) as con:
            rows = con.execute(
                """SELECT ef.id,ef.initial_intent_id,ef.due_at,ai.payload,
                          er.message_id,er.sent_at
                FROM email_followups ef
                JOIN action_intents ai ON ai.id=ef.initial_intent_id
                JOIN email_receipts er ON er.action_intent_id=ef.initial_intent_id
                WHERE ef.state='pending' AND ef.due_at<=?
                ORDER BY ef.due_at,ef.id LIMIT ?""",
                (timestamp, limit),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    def prepare_due_email_followup(
        self,
        *,
        followup_id: int,
        run_id: str,
        body: str,
        now: str | datetime,
    ) -> EmailIntentReservation:
        text = str(body).strip()
        if not text or len(text) > 100_000:
            raise ValueError("follow-up body must be 1..100000 characters")
        timestamp = parse_time(now).isoformat()
        with closing(self._connect()) as con:
            row = con.execute(
                """SELECT ef.id,ef.state,ef.due_at,ef.initial_intent_id,
                          ef.followup_intent_id,ai.payload
                FROM email_followups ef
                JOIN action_intents ai ON ai.id=ef.initial_intent_id
                WHERE ef.id=?""",
                (followup_id,),
            ).fetchone()
        if row is None:
            raise ValueError("unknown email follow-up")
        if row["state"] not in {"pending", "reserved"}:
            raise IntentFenceViolation("follow-up is not sendable")
        if parse_time(row["due_at"]) > parse_time(now):
            raise IntentFenceViolation("follow-up is not due")
        initial_payload = json.loads(row["payload"])
        initial_message_id = initial_payload["message_id"]
        initial_subject = str(initial_payload["subject"])
        subject = initial_subject if initial_subject.lower().startswith("re:") else f"Re: {initial_subject}"
        canonical_base = {
            "sender": initial_payload["sender"],
            "recipient": initial_payload["recipient"],
            "recipient_provenance": initial_payload["recipient_provenance"],
            "subject": subject,
            "body": text,
            "in_reply_to": initial_message_id,
            "references": initial_message_id,
            "initial_intent_id": int(row["initial_intent_id"]),
            "followup_id": int(followup_id),
        }
        canonical_content = json.dumps(
            canonical_base, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(canonical_content.encode("utf-8")).hexdigest()
        domain = str(initial_payload["sender"]).rsplit("@", 1)[1].lower()
        message_id = f"<job-search-followup-{digest[:40]}@{domain}>"
        payload = dict(canonical_base)
        payload["message_id"] = message_id
        payload["content_sha256"] = digest
        key_digest = hashlib.sha256(
            f"followup:{row['initial_intent_id']}".encode("utf-8")
        ).hexdigest()
        reserved = self.reserve_action_intent(
            run_id=run_id,
            kind="email_followup",
            idempotency_key=f"email-followup:{key_digest}",
            payload=payload,
            now=timestamp,
        )
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            current = con.execute(
                "SELECT state,followup_intent_id FROM email_followups WHERE id=?",
                (followup_id,),
            ).fetchone()
            if current is None or current["state"] not in {"pending", "reserved"}:
                con.rollback()
                raise IntentFenceViolation("follow-up changed while being reserved")
            if current["followup_intent_id"] not in {None, reserved.intent_id}:
                con.rollback()
                raise ActionIntentConflict("follow-up intent conflict")
            con.execute(
                """UPDATE email_followups SET state='reserved',followup_intent_id=?,updated_at=?
                WHERE id=?""",
                (reserved.intent_id, timestamp, followup_id),
            )
            con.commit()
        return EmailIntentReservation(
            intent_id=reserved.intent_id,
            created=reserved.created,
            message_id=message_id,
        )

    def mark_email_response(
        self,
        *,
        initial_intent_id: int,
        response_uid: str,
        responded_at: str | datetime,
    ) -> None:
        uid = str(response_uid).strip()
        if not uid or len(uid) > 512:
            raise ValueError("invalid response UID")
        timestamp = parse_time(responded_at).isoformat()
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT state,response_uid,responded_at FROM email_followups WHERE initial_intent_id=?",
                (initial_intent_id,),
            ).fetchone()
            if row is None:
                con.rollback()
                raise ValueError("no follow-up schedule for initial email")
            if row["state"] == "cancelled":
                if row["response_uid"] != uid or row["responded_at"] != timestamp:
                    con.rollback()
                    raise ActionIntentConflict("email response evidence conflict")
                con.commit()
                return
            if row["state"] != "pending":
                con.rollback()
                raise IntentFenceViolation("follow-up is already being executed")
            con.execute(
                """UPDATE email_followups
                SET state='cancelled',response_uid=?,responded_at=?,updated_at=?
                WHERE initial_intent_id=? AND state='pending'""",
                (uid, timestamp, timestamp, initial_intent_id),
            )
            con.commit()

    def reserve_action_intent(
        self,
        *,
        run_id: str,
        kind: str,
        idempotency_key: str,
        payload: dict[str, Any],
        now: str | datetime,
    ) -> ActionIntentReservation:
        kind = source_name(kind)
        key = str(idempotency_key).strip()
        if not key or len(key) > 512:
            raise ValueError("invalid idempotency_key")
        try:
            canonical_payload = json.dumps(
                payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("payload must be finite JSON") from exc
        if len(canonical_payload.encode("utf-8")) > 65536:
            raise ValueError("payload is too large")
        timestamp = parse_time(now).isoformat()
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            control = con.execute(
                "SELECT paused,reason FROM workflow_control WHERE singleton=1"
            ).fetchone()
            if control["paused"]:
                con.rollback()
                raise WorkflowPaused(control["reason"])
            run = con.execute("SELECT state,max_actions FROM batch_runs WHERE id=?", (run_id,)).fetchone()
            if run is None:
                con.rollback()
                raise ValueError("unknown batch run")
            if run["state"] != "running":
                con.rollback()
                raise ValueError("batch run is not running")
            existing = con.execute(
                "SELECT id,payload FROM action_intents WHERE kind=? AND idempotency_key=?",
                (kind, key),
            ).fetchone()
            if existing is not None:
                if existing["payload"] != canonical_payload:
                    con.rollback()
                    raise ActionIntentConflict("idempotency key payload conflict")
                con.commit()
                return ActionIntentReservation(intent_id=int(existing["id"]), created=False)
            if kind == "application_submit" and payload.get("source") == "hh":
                day = timestamp[:10]
                used = int(con.execute(
                    "SELECT count(*) FROM application_receipts WHERE source='hh' AND submitted=1 AND read_back_verified=1 AND substr(submitted_at,1,10)=?",
                    (day,),
                ).fetchone()[0])
                active = int(con.execute(
                    """SELECT count(*) FROM action_intents
                       WHERE kind='application_submit' AND state IN ('reserved','executing','ambiguous')
                         AND json_extract(payload,'$.source')='hh'
                         AND substr(COALESCE(side_effect_maybe_at,execution_started_at,created_at),1,10)=?""",
                    (day,),
                ).fetchone()[0])
                daily_cap = payload.get("daily_cap")
                if isinstance(daily_cap, bool) or not isinstance(daily_cap, int) or daily_cap < 1:
                    con.rollback()
                    raise ValueError("HH intent requires a positive daily_cap")
                if used + active >= daily_cap:
                    con.rollback()
                    raise BatchQuotaExceeded("daily HH application limit reached")
            quota = con.execute(
                "SELECT max_actions FROM batch_runs WHERE id=?", (run_id,)
            ).fetchone()
            reserved = con.execute(
                "SELECT count(*) FROM action_intents WHERE run_id=?", (run_id,)
            ).fetchone()[0]
            if int(reserved) >= int(quota["max_actions"]):
                con.rollback()
                raise BatchQuotaExceeded("batch action limit reached")
            cursor = con.execute(
                """INSERT INTO action_intents
                (run_id,kind,idempotency_key,payload,state,created_at,updated_at)
                VALUES(?,?,?,?,'reserved',?,?)
                ON CONFLICT(kind,idempotency_key) DO NOTHING""",
                (run_id, kind, key, canonical_payload, timestamp, timestamp),
            )
            created = cursor.rowcount == 1
            row = con.execute(
                "SELECT id,payload FROM action_intents WHERE kind=? AND idempotency_key=?",
                (kind, key),
            ).fetchone()
            assert row is not None
            if row["payload"] != canonical_payload:
                con.rollback()
                raise ActionIntentConflict("idempotency key payload conflict")
            con.commit()
            return ActionIntentReservation(intent_id=int(row["id"]), created=created)

    def mark_intent_executing(
        self, *, intent_id: int, worker_id: str, now: str | datetime
    ) -> str:
        worker = str(worker_id).strip()
        if not worker or len(worker) > 128:
            raise ValueError("invalid worker_id")
        timestamp = parse_time(now).isoformat()
        token = uuid.uuid4().hex
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            updated = con.execute(
                """UPDATE action_intents
                SET state='executing',executing_by=?,execution_token=?,
                    execution_started_at=?,updated_at=?,last_error_code=NULL
                WHERE id=? AND state='reserved'
                  AND EXISTS (SELECT 1 FROM workflow_control WHERE singleton=1 AND paused=0)
                  AND EXISTS (SELECT 1 FROM batch_runs br WHERE br.id=action_intents.run_id AND br.state='running')""",
                (worker, token, timestamp, timestamp, intent_id),
            )
            if updated.rowcount != 1:
                con.rollback()
                raise IntentFenceViolation("intent is not reservable for execution")
            con.commit()
        return token

    def assert_intent_execution_fence(self, *, intent_id: int, execution_token: str) -> None:
        token = str(execution_token).strip()
        if not token:
            raise IntentFenceViolation("missing execution token")
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                """SELECT 1 FROM action_intents ai
                JOIN batch_runs br ON br.id=ai.run_id
                JOIN workflow_control wc ON wc.singleton=1
                WHERE ai.id=? AND ai.state='executing' AND ai.execution_token=?
                  AND br.state='running' AND wc.paused=0""",
                (intent_id, token),
            ).fetchone()
            if row is None:
                con.rollback()
                raise IntentFenceViolation("execution fence is no longer valid")
            con.commit()

    def execution_intent_payload(
        self, *, intent_id: int, execution_token: str = "", reconciliation_token: str = ""
    ) -> dict[str, Any]:
        token = str(execution_token).strip()
        reconcile = str(reconciliation_token).strip()
        with closing(self._connect()) as con:
            if token:
                row = con.execute(
                    "SELECT payload,execution_started_at FROM action_intents WHERE id=? AND state='executing' AND execution_token=?",
                    (intent_id, token),
                ).fetchone()
            else:
                row = con.execute(
                    """SELECT payload,COALESCE(execution_started_at,side_effect_maybe_at) AS execution_started_at
                       FROM action_intents WHERE id=? AND state='ambiguous' AND reconciliation_token=?""",
                    (intent_id, reconcile),
                ).fetchone()
        if row is None:
            raise IntentFenceViolation("stale token or intent is not executing")
        payload = json.loads(row["payload"])
        payload["_execution_started_at"] = row["execution_started_at"]
        return payload

    def close_execution_blocked(
        self,
        *,
        intent_id: int,
        execution_token: str,
        now: str | datetime,
        error_code: str,
    ) -> None:
        """Close a proven pre-side-effect execution without creating ambiguity or replay."""
        token = str(execution_token).strip()
        code = source_name(error_code)
        if not token:
            raise IntentFenceViolation("missing execution token")
        timestamp = parse_time(now).isoformat()
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            updated = con.execute(
                """UPDATE action_intents SET state='blocked',last_error_code=?,updated_at=?,
                   executing_by=NULL,execution_token=NULL
                   WHERE id=? AND state='executing' AND execution_token=? AND side_effect_maybe_at IS NULL""",
                (code,timestamp,intent_id,token),
            )
            if updated.rowcount != 1:
                con.rollback();raise IntentFenceViolation("stale token or side effect may have occurred")
            con.commit()

    def recover_stale_executions(
        self,
        *,
        now: str | datetime,
        older_than_seconds: int,
        source: str | None = None,
    ) -> list[int]:
        """Conservatively fence crashed workers without replaying their side effects."""
        if isinstance(older_than_seconds, bool) or not isinstance(older_than_seconds, int) or older_than_seconds < 1:
            raise ValueError("older_than_seconds must be a positive integer")
        timestamp = parse_time(now)
        cutoff = (timestamp - timedelta(seconds=older_than_seconds)).isoformat()
        params: list[Any] = [timestamp.isoformat(), timestamp.isoformat(), cutoff]
        source_filter = ""
        if source is not None:
            normalized = source_name(source)
            source_filter = " AND json_extract(payload,'$.source')=?"
            params.append(normalized)
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                """SELECT id FROM action_intents
                   WHERE state='executing' AND execution_started_at<=?""" + source_filter + " ORDER BY id",
                tuple([cutoff] + params[3:]),
            ).fetchall()
            ids = [int(row["id"]) for row in rows]
            if ids:
                marks = ",".join("?" for _ in ids)
                con.execute(
                    f"""UPDATE action_intents
                        SET state='ambiguous',side_effect_maybe_at=?,last_error_code='worker_crash_unknown_side_effect',
                            updated_at=?,executing_by=NULL,execution_token=NULL
                        WHERE state='executing' AND id IN ({marks})""",
                    (timestamp.isoformat(), timestamp.isoformat(), *ids),
                )
            con.commit()
        return ids

    def mark_intent_ambiguous(
        self,
        *,
        intent_id: int,
        execution_token: str,
        now: str | datetime,
        error_code: str,
    ) -> None:
        token = str(execution_token).strip()
        code = source_name(error_code)
        if not token:
            raise IntentFenceViolation("missing execution token")
        timestamp = parse_time(now).isoformat()
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            updated = con.execute(
                """UPDATE action_intents
                SET state='ambiguous',side_effect_maybe_at=?,last_error_code=?,updated_at=?,
                    executing_by=NULL,execution_token=NULL
                WHERE id=? AND state='executing' AND execution_token=?""",
                (timestamp, code, timestamp, intent_id, token),
            )
            if updated.rowcount != 1:
                con.rollback()
                raise IntentFenceViolation("stale token or intent is not executing")
            con.commit()

    def claim_due_reconciliations(
        self,
        *,
        worker_id: str,
        limit: int,
        now: str | datetime,
    ) -> list[ReconciliationClaim]:
        worker = str(worker_id).strip()
        if not worker or len(worker) > 128:
            raise ValueError("invalid worker_id")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        timestamp = parse_time(now).isoformat()
        claims: list[ReconciliationClaim] = []
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                """SELECT id,payload,reconciliation_attempts
                FROM action_intents
                WHERE state='ambiguous' AND reconciliation_token IS NULL
                  AND (next_reconcile_at IS NULL OR next_reconcile_at<=?)
                ORDER BY COALESCE(next_reconcile_at,side_effect_maybe_at),id
                LIMIT ?""",
                (timestamp, limit),
            ).fetchall()
            for row in rows:
                token = uuid.uuid4().hex
                attempt = int(row["reconciliation_attempts"]) + 1
                updated = con.execute(
                    """UPDATE action_intents
                    SET reconciling_by=?,reconciliation_token=?,reconciliation_attempts=?,updated_at=?
                    WHERE id=? AND state='ambiguous' AND reconciliation_token IS NULL""",
                    (worker, token, attempt, timestamp, int(row["id"])),
                )
                if updated.rowcount != 1:
                    continue
                claims.append(
                    ReconciliationClaim(
                        intent_id=int(row["id"]),
                        payload=json.loads(row["payload"]),
                        reconciliation_token=token,
                        reconciliation_attempt=attempt,
                    )
                )
            con.commit()
        return claims

    def reschedule_reconciliation(
        self,
        *,
        intent_id: int,
        reconciliation_token: str,
        next_reconcile_at: str | datetime,
        now: str | datetime,
        error_code: str,
    ) -> None:
        token = str(reconciliation_token).strip()
        if not token:
            raise IntentFenceViolation("missing reconciliation token")
        timestamp = parse_time(now)
        next_at = parse_time(next_reconcile_at)
        if next_at <= timestamp:
            raise ValueError("next reconciliation must be in the future")
        code = source_name(error_code)
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            updated = con.execute(
                """UPDATE action_intents
                SET reconciling_by=NULL,reconciliation_token=NULL,next_reconcile_at=?,
                    last_error_code=?,updated_at=?
                WHERE id=? AND state='ambiguous' AND reconciliation_token=?""",
                (next_at.isoformat(), code, timestamp.isoformat(), intent_id, token),
            )
            if updated.rowcount != 1:
                con.rollback()
                raise IntentFenceViolation("stale reconciliation token or unsafe state")
            con.commit()

    def close_reconciliation_blocked(
        self,
        *,
        intent_id: int,
        reconciliation_token: str,
        now: str | datetime,
        error_code: str,
    ) -> None:
        """Close an ambiguous intent without retry after conclusive or exhausted read-back.

        This transition never creates a receipt and never authorizes a replay.
        """
        token = str(reconciliation_token).strip()
        if not token:
            raise IntentFenceViolation("missing reconciliation token")
        timestamp = parse_time(now).isoformat()
        code = source_name(error_code)
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            updated = con.execute(
                """UPDATE action_intents
                SET state='blocked',reconciling_by=NULL,reconciliation_token=NULL,
                    next_reconcile_at=NULL,last_error_code=?,updated_at=?
                WHERE id=? AND state='ambiguous' AND reconciliation_token=?""",
                (code, timestamp, intent_id, token),
            )
            if updated.rowcount != 1:
                con.rollback()
                raise IntentFenceViolation("stale reconciliation token or unsafe state")
            con.commit()

    def has_verified_application_receipt(self, *, source: str, external_id: str) -> bool:
        """Authoritative pre-submit dedupe across imported and native receipts."""
        with closing(self._connect()) as con:
            return con.execute(
                """SELECT 1 FROM application_receipts
                   WHERE source=? AND external_vacancy_id=? AND read_back_verified=1
                   LIMIT 1""",
                (str(source).strip(), str(external_id).strip()),
            ).fetchone() is not None

    def _link_existing_receipt(self, job_id: int, source: str, external_id: str) -> bool:
        with closing(self._connect()) as con:
            row = con.execute(
                "SELECT id FROM application_receipts WHERE source=? AND external_vacancy_id=? AND submitted=1",
                (source, external_id),
            ).fetchone()
            if row is None:
                return False
            con.execute("UPDATE application_receipts SET job_id=? WHERE id=?", (job_id, int(row["id"])))
            con.commit()
            return True

    def import_rows(self, rows: Iterable[dict[str, Any]]) -> dict[str, int]:
        result = {"accepted": 0, "created": 0, "updated": 0, "duplicates": 0, "queued": 0, "rejected": 0}
        for row in rows:
            try:
                source = source_name(row.get("source"))
                url = canonical_url(row.get("job_url") or row.get("url"))
                external_id = external_id_for(
                    source, url, row.get("external_vacancy_id") or row.get("job_id")
                )
                title = str(row.get("job_title") or row.get("title") or "").strip()
                if not title:
                    raise ValueError("missing title")
                company = str(row.get("company") or "Не указана").strip() or "Не указана"
                score = score_of(row)
                metadata = {
                    key: row.get(key)
                    for key in (
                        "fit_score", "status", "next_action", "why_relevant", "salary",
                        "published_at", "remote_location", "hr_email", "recruiter_name",
                        "recruiter_linkedin", "contact_source", "enrichment_status",
                    )
                    if row.get(key) not in (None, "")
                }
                outcome = self.store.upsert_job(Job(
                    source=source,
                    external_id=external_id,
                    title=title,
                    company=company,
                    url=url,
                    location=str(row.get("remote_location") or row.get("location") or "") or None,
                    description=str(row.get("description") or row.get("notes") or "") or None,
                    metadata=metadata,
                ))
                result["accepted"] += 1
                result["created"] += int(outcome.created)
                result["updated"] += int(outcome.updated)
                result["duplicates"] += int(outcome.duplicate)
                already_applied = self._link_existing_receipt(outcome.job_id, source, external_id)
                if outcome.created and score >= HIGH_FIT_THRESHOLD and not already_applied:
                    base = {
                        "source": source,
                        "external_id": external_id,
                        "job_url": url,
                        "company": company,
                        "job_title": title,
                        "fit_score": score,
                    }
                    self.store.enqueue(
                        "application_review", base,
                        idempotency_key=f"{source}:{external_id}", max_attempts=3,
                    )
                    result["queued"] += 1
                    recipient = str(row.get("hr_email") or "").strip()
                    if recipient:
                        self.store.enqueue(
                            "email_outreach_draft", {**base, "recipient": recipient},
                            idempotency_key=f"{source}:{external_id}:{recipient.lower()}",
                            max_attempts=3,
                        )
                        result["queued"] += 1
            except (TypeError, ValueError, IdempotencyConflict):
                result["rejected"] += 1
        return result

    def record_application(self, **kwargs: Any) -> int:
        if "strict_evidence_verified" in kwargs:
            raise TypeError("strict evidence capability is not part of the public API")
        source = source_name(str(kwargs.get("source") or ""))
        status = str(kwargs.get("status") or "").strip().lower()
        if source == "hh" and status in FOLLOWUP_ELIGIBLE_STATUSES:
            raise IntentFenceViolation("HH submitted statuses require strict evidence bridge")
        return self._record_application(**kwargs)

    def _record_application(
        self,
        *,
        source: str,
        external_vacancy_id: str,
        job_url: str,
        company: str,
        job_title: str,
        status: str,
        submitted_at: str | None,
        read_back_verified: bool,
        evidence_path: str,
        channel: str = "platform",
        response_at: str | None = None,
        intent_id: int | None = None,
        execution_token: str | None = None,
        reconciliation_token: str | None = None,
        strict_evidence_verified: bool = False,
    ) -> int:
        source = source_name(source)
        job_url = canonical_url(job_url)
        external_vacancy_id = external_id_for(source, job_url, external_vacancy_id)
        status = status.strip().lower()
        if not status:
            raise ValueError("status is required")
        now = utc_now().isoformat()
        submitted = int(bool(submitted_at) and bool(read_back_verified) and status in FOLLOWUP_ELIGIBLE_STATUSES)
        if source == "hh" and submitted and intent_id is None:
            raise IntentFenceViolation("submitted HH receipt requires strict intent-backed evidence bridge")
        with closing(self._connect()) as con:
            con.execute("BEGIN IMMEDIATE")
            run_id: str | None = None
            intent_payload: dict[str, Any] = {}
            if intent_id is not None:
                intent = con.execute(
                    "SELECT run_id,payload,state,execution_token,reconciliation_token FROM action_intents WHERE id=?", (intent_id,)
                ).fetchone()
                if intent is None:
                    con.rollback()
                    raise ValueError("unknown action intent")
                run_id = str(intent["run_id"])
                if source == "hh" and not strict_evidence_verified:
                    con.rollback()
                    raise IntentFenceViolation("HH intent receipt requires strict evidence bridge")
                intent_payload = json.loads(intent["payload"])
                if (
                    intent_payload.get("source") != source
                    or str(intent_payload.get("external_id")) != external_vacancy_id
                ):
                    con.rollback()
                    raise ActionIntentConflict("receipt identity does not match intent")
                if intent["state"] == "executing":
                    if not execution_token or intent["execution_token"] != execution_token:
                        con.rollback()
                        raise IntentFenceViolation("valid execution token is required")
                elif intent["state"] == "ambiguous":
                    if source == "hh" and not strict_evidence_verified:
                        con.rollback()
                        raise IntentFenceViolation("HH reconciliation requires strict evidence bridge")
                    if not reconciliation_token or intent["reconciliation_token"] != reconciliation_token:
                        con.rollback()
                        raise IntentFenceViolation("valid reconciliation token is required")
                elif intent["state"] != "verified":
                    con.rollback()
                    raise IntentFenceViolation("intent must be executing, reconciling, or already verified")
                run_id = str(intent["run_id"])
            job = con.execute(
                "SELECT id FROM jobs WHERE source=? AND external_id=?",
                (source, external_vacancy_id),
            ).fetchone()
            if source == "hh" and submitted and intent_id is not None:
                cap_row = con.execute("SELECT max_actions FROM batch_runs WHERE id=?", (run_id,)).fetchone()
                day = str(submitted_at)[:10]
                other_receipts = int(con.execute(
                    """SELECT count(*) FROM application_receipts
                       WHERE source='hh' AND submitted=1 AND read_back_verified=1
                         AND substr(submitted_at,1,10)=? AND external_vacancy_id<>?""",
                    (day, external_vacancy_id),
                ).fetchone()[0])
                other_active = int(con.execute(
                    """SELECT count(*) FROM action_intents
                       WHERE id<>? AND kind='application_submit' AND state IN ('reserved','executing','ambiguous')
                         AND json_extract(payload,'$.source')='hh'
                         AND substr(COALESCE(side_effect_maybe_at,execution_started_at,created_at),1,10)=?""",
                    (intent_id, day),
                ).fetchone()[0])
                daily_cap = intent_payload.get("daily_cap")
                if isinstance(daily_cap, bool) or not isinstance(daily_cap, int) or daily_cap < 1:
                    con.rollback()
                    raise ValueError("HH intent requires a positive daily_cap")
                if cap_row is None or other_receipts + other_active + 1 > daily_cap:
                    con.rollback()
                    raise BatchQuotaExceeded("verified receipt would exceed daily HH application limit")
            con.execute(
                """INSERT INTO application_receipts
                (job_id,job_url,external_vacancy_id,source,company,job_title,channel,status,
                 submitted,read_back_verified,submitted_at,response_at,evidence_path,updated_at,
                 run_id,action_intent_id)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source,external_vacancy_id) DO UPDATE SET
                  job_id=COALESCE(excluded.job_id,application_receipts.job_id),
                  job_url=excluded.job_url,company=excluded.company,job_title=excluded.job_title,
                  channel=excluded.channel,status=excluded.status,
                  submitted=MAX(application_receipts.submitted,excluded.submitted),
                  read_back_verified=MAX(application_receipts.read_back_verified,excluded.read_back_verified),
                  submitted_at=COALESCE(application_receipts.submitted_at,excluded.submitted_at),
                  response_at=COALESCE(excluded.response_at,application_receipts.response_at),
                  evidence_path=COALESCE(excluded.evidence_path,application_receipts.evidence_path),
                  updated_at=excluded.updated_at,
                  run_id=COALESCE(application_receipts.run_id,excluded.run_id),
                  action_intent_id=COALESCE(application_receipts.action_intent_id,excluded.action_intent_id)""",
                (
                    int(job["id"]) if job else None, job_url, external_vacancy_id, source,
                    company.strip() or "Не указана", job_title.strip() or "Не указана", channel,
                    status, submitted, int(bool(read_back_verified)), submitted_at, response_at,
                    evidence_path or None, now, run_id, intent_id,
                ),
            )
            receipt = con.execute(
                "SELECT id FROM application_receipts WHERE source=? AND external_vacancy_id=?",
                (source, external_vacancy_id),
            ).fetchone()
            assert receipt is not None
            event_at = response_at or submitted_at or now
            con.execute(
                "INSERT OR IGNORE INTO status_events(receipt_id,at,status,detail) VALUES(?,?,?,?)",
                (int(receipt["id"]), event_at, status, evidence_path or ""),
            )
            if intent_id is not None and submitted and read_back_verified:
                updated = con.execute(
                    """UPDATE action_intents
                    SET state='verified',updated_at=?,executing_by=NULL,execution_token=NULL,
                        reconciling_by=NULL,reconciliation_token=NULL,next_reconcile_at=NULL,
                        last_error_code=NULL
                    WHERE id=? AND state IN ('reserved','executing','ambiguous','verified')""",
                    (now, intent_id),
                )
                if updated.rowcount != 1:
                    con.rollback()
                    raise ValueError("action intent transition conflict")
            if submitted and read_back_verified:
                con.execute(
                    """UPDATE queue SET state='done', last_error='superseded_by_application_receipt'
                    WHERE state='pending' AND kind IN ('application_review','email_outreach_draft')
                      AND json_extract(payload,'$.source')=?
                      AND json_extract(payload,'$.external_id')=?""",
                    (source, external_vacancy_id),
                )
            con.commit()
            return int(receipt["id"])

    def enqueue_due_followups(self, *, now: str | datetime, after_days: int = 5) -> int:
        current = parse_time(now)
        cutoff = current - timedelta(days=after_days)
        with closing(self._connect()) as con:
            rows = con.execute(
                """SELECT * FROM application_receipts
                WHERE submitted=1 AND read_back_verified=1 AND submitted_at IS NOT NULL
                  AND status IN ('submitted','applied','seen','viewed','screening')
                  AND submitted_at <= ? ORDER BY id""",
                (cutoff.isoformat(),),
            ).fetchall()
        created = 0
        for row in rows:
            before = self.store.count_queue_items()
            self.store.enqueue(
                "application_followup",
                {
                    "source": row["source"],
                    "external_id": row["external_vacancy_id"],
                    "job_url": row["job_url"],
                    "company": row["company"],
                    "job_title": row["job_title"],
                    "receipt_id": int(row["id"]),
                },
                idempotency_key=f"{row['source']}:{row['external_vacancy_id']}:followup:1",
                now=current,
                max_attempts=3,
            )
            created += int(self.store.count_queue_items() > before)
        return created

    def summary(self) -> dict[str, Any]:
        with closing(self._connect()) as con:
            queue = {row["state"]: int(row["n"]) for row in con.execute(
                "SELECT state,count(*) n FROM queue GROUP BY state ORDER BY state"
            )}
            applications = {row["status"]: int(row["n"]) for row in con.execute(
                "SELECT status,count(*) n FROM application_receipts GROUP BY status ORDER BY status"
            )}
        return {"db": str(self.path), "jobs": self.store.count_jobs(), "queue": queue, "applications": applications}


def load_json_files(paths: Iterable[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw_path in paths:
        path = Path(raw_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        rows.extend(rows_from_payload(payload))
    return rows


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    root.add_argument("--db", default=str(DEFAULT_DB))
    sub = root.add_subparsers(dest="command", required=True)
    imp = sub.add_parser("import-json")
    imp.add_argument("paths", nargs="+")
    sub.add_parser("status")
    follow = sub.add_parser("enqueue-followups")
    follow.add_argument("--now", default=utc_now().isoformat())
    follow.add_argument("--after-days", type=int, default=5)
    return root


def main() -> int:
    args = parser().parse_args()
    with LocalFunnel(args.db) as funnel:
        if args.command == "import-json":
            result = funnel.import_rows(load_json_files(args.paths))
            output = {**result, "summary": funnel.summary()}
        elif args.command == "enqueue-followups":
            output = {"created": funnel.enqueue_due_followups(now=args.now, after_days=args.after_days), "summary": funnel.summary()}
        else:
            output = funnel.summary()
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
