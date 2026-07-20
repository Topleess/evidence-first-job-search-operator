#!/usr/bin/env python3
"""HH supported-route state adapter.

This module is deliberately side-effect fenced: it can collect browser observations,
prepare durable application intents, and record read-back evidence, but it cannot
submit an HH response. Production job_funnel.sqlite3 is explicitly rejected.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PRODUCTION_DB = Path("/opt/data/job-search/state/job_funnel.sqlite3").resolve()
APPLIED_RE = re.compile(r"Вы\s*откликнулись|Ваш отклик отправлен работодателю|Отклик отправлен|Резюме доставлено", re.I)
CLOSED_RE = re.compile(r"вакансия в архиве|Вакансия закрыта|вакансия больше не доступна", re.I)
APPLY_RE = re.compile(r"Откликнуться|Подать заявку", re.I)
LOGIN_URL_RE = re.compile(r"/account/login|/account/signup", re.I)
LOGIN_FORM_RE = re.compile(r"Введите телефон|Вход для соискателей|Войдите в аккаунт", re.I)
ACCOUNT_RE = re.compile(r"Мои резюме|Отклики и приглашения|Создать резюме", re.I)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def classify_auth(url: str, text: str) -> str:
    if LOGIN_URL_RE.search(url) or LOGIN_FORM_RE.search(text):
        return "login_required"
    if ACCOUNT_RE.search(text):
        return "authenticated"
    return "unknown"


def classify_vacancy(text: str) -> str:
    if APPLIED_RE.search(text):
        return "already_applied"
    if CLOSED_RE.search(text):
        return "closed"
    if APPLY_RE.search(text):
        return "available"
    return "unknown"


def canonical_request(request: dict[str, Any]) -> dict[str, str]:
    result = {
        "vacancy_id": str(request.get("vacancy_id") or "").strip(),
        "url": str(request.get("url") or "").strip(),
        "resume_id": str(request.get("resume_id") or "").strip(),
        "cover_sha256": str(request.get("cover_sha256") or "").strip(),
    }
    if not result["vacancy_id"] or not re.fullmatch(r"https://(?:[^/]+\.)?hh\.(?:ru|kz)/vacancy/\d+(?:[/?#].*)?", result["url"]):
        raise ValueError("valid HH vacancy_id and hh.ru/hh.kz vacancy URL are required")
    return result


class HHState:
    def __init__(self, db_path: Path | str):
        self.path = Path(db_path).resolve()
        if self.path == PRODUCTION_DB:
            raise ValueError("production job_funnel.sqlite3 is fenced from the HH agent")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.path, timeout=10)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA busy_timeout=10000")
        self.con.executescript("""
        CREATE TABLE IF NOT EXISTS hh_application_intents (
          intent_id TEXT PRIMARY KEY,
          vacancy_id TEXT NOT NULL,
          url TEXT NOT NULL,
          resume_id TEXT NOT NULL,
          cover_sha256 TEXT NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('prepared','blocked','cancelled')),
          submitted INTEGER NOT NULL DEFAULT 0 CHECK(submitted = 0),
          created_at TEXT NOT NULL,
          UNIQUE(vacancy_id, resume_id, cover_sha256)
        );
        CREATE TABLE IF NOT EXISTS hh_readback_receipts (
          vacancy_id TEXT PRIMARY KEY,
          url TEXT NOT NULL,
          vacancy_status TEXT NOT NULL CHECK(vacancy_status = 'already_applied'),
          evidence_sha256 TEXT NOT NULL,
          final_url TEXT NOT NULL,
          read_back_verified INTEGER NOT NULL CHECK(read_back_verified = 1),
          submitted_by_adapter INTEGER NOT NULL DEFAULT 0 CHECK(submitted_by_adapter = 0),
          observed_at TEXT NOT NULL
        );
        """)
        self.con.commit()

    def __enter__(self) -> "HHState":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.con.close()

    def create_intent(self, request: dict[str, Any]) -> dict[str, Any]:
        req = canonical_request(request)
        fingerprint = json.dumps(req, sort_keys=True, ensure_ascii=False).encode()
        intent_id = "hh:" + hashlib.sha256(fingerprint).hexdigest()[:24]
        now = utc_now()
        with self.con:
            self.con.execute(
                "INSERT OR IGNORE INTO hh_application_intents "
                "(intent_id,vacancy_id,url,resume_id,cover_sha256,status,created_at) VALUES (?,?,?,?,?,'prepared',?)",
                (intent_id, req["vacancy_id"], req["url"], req["resume_id"], req["cover_sha256"], now),
            )
        row = self.con.execute("SELECT * FROM hh_application_intents WHERE intent_id=?", (intent_id,)).fetchone()
        return dict(row)

    def record_observation(self, observation: dict[str, Any]) -> dict[str, Any]:
        vacancy_id = str(observation.get("vacancy_id") or "").strip()
        url = str(observation.get("url") or "").strip()
        status = str(observation.get("vacancy_status") or "")
        final_url = str(observation.get("final_url") or "")
        evidence_sha256 = str(observation.get("evidence_sha256") or "")
        canonical_request({"vacancy_id": vacancy_id, "url": url, "resume_id": "readback", "cover_sha256": "readback"})
        if status != "already_applied" or not evidence_sha256 or not re.match(r"https://(?:[^/]+\.)?hh\.(?:ru|kz)/", final_url):
            return {"read_back_verified": False, "submitted_by_adapter": False, "reason": "insufficient_read_back_evidence"}
        with self.con:
            self.con.execute(
                "INSERT INTO hh_readback_receipts VALUES (?,?,?,?,?,1,0,?) "
                "ON CONFLICT(vacancy_id) DO UPDATE SET evidence_sha256=excluded.evidence_sha256, final_url=excluded.final_url, observed_at=excluded.observed_at",
                (vacancy_id, url, status, evidence_sha256, final_url, utc_now()),
            )
        return {"vacancy_id": vacancy_id, "read_back_verified": True, "submitted_by_adapter": False}

    def summary(self) -> dict[str, int]:
        return {
            "intents": self.con.execute("SELECT count(*) FROM hh_application_intents").fetchone()[0],
            "receipts": self.con.execute("SELECT count(*) FROM hh_readback_receipts").fetchone()[0],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare HH dry-run intents or ingest read-only browser observations")
    parser.add_argument("--db", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    intent = sub.add_parser("prepare-intent")
    intent.add_argument("--input", required=True)
    observe = sub.add_parser("record-observation")
    observe.add_argument("--input", required=True)
    sub.add_parser("summary")
    args = parser.parse_args()
    with HHState(args.db) as state:
        if args.command == "prepare-intent":
            result = state.create_intent(json.loads(Path(args.input).read_text(encoding="utf-8")))
        elif args.command == "record-observation":
            result = state.record_observation(json.loads(Path(args.input).read_text(encoding="utf-8")))
        else:
            result = state.summary()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
