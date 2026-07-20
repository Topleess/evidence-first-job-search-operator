#!/usr/bin/env python3
"""Export a redacted, aggregate-only public evidence package from authoritative state."""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRET_PATTERNS = [
    re.compile(r"bearer\s+[a-z0-9._~-]+", re.I),
    re.compile(r"(?:access|refresh|id)[_-]?token", re.I),
    re.compile(r"client[_-]?secret", re.I),
    re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I),
]


def build(db_path: Path) -> dict:
    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        channels = {}
        for channel in ("hh", "linkedin", "ats", "email"):
            runs = con.execute(
                "SELECT id,state,max_actions,started_at,finished_at FROM batch_runs WHERE channel=? ORDER BY started_at DESC LIMIT 2",
                (channel,),
            ).fetchall()
            channels[channel] = {"recent_runs": [dict(r) for r in runs]}
        receipts = con.execute(
            """SELECT id,source,external_vacancy_id,status,submitted,read_back_verified,
                      submitted_at,run_id,action_intent_id
                 FROM application_receipts
                WHERE read_back_verified=1
                ORDER BY id DESC"""
        ).fetchall()
        email_count = con.execute("SELECT count(*) FROM email_receipts WHERE read_back_verified=1").fetchone()[0]
        ambiguous = con.execute("SELECT count(*) FROM action_intents WHERE state='ambiguous'").fetchone()[0]
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    return {
        "schema_version": "job_search_public_case.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "authoritative_workspace": "/opt/data/job-search",
        "authoritative_database": "state/job_funnel.sqlite3",
        "safety_contract": {
            "durable_intent_before_side_effect": True,
            "independent_read_back_required": True,
            "ambiguous_outcome_not_retried": True,
            "ceilings": {"hh": 20, "linkedin": 5, "email": 5, "ats": 1},
        },
        "database_integrity": integrity,
        "ambiguous_intents": ambiguous,
        "verified_application_receipts": [dict(r) for r in receipts],
        "verified_email_receipt_count": email_count,
        "channels": channels,
        "redaction": {
            "pii_included": False,
            "secrets_included": False,
            "excluded": ["candidate identity", "email addresses", "message bodies", "OAuth tokens", "browser profiles", "execution tokens", "intent payloads"],
        },
    }


def validate(payload: dict) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    for pattern in SECRET_PATTERNS:
        if pattern.search(text):
            raise ValueError(f"redaction validation failed: {pattern.pattern}")
    if payload.get("database_integrity") != "ok":
        raise ValueError("authoritative database integrity check failed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=ROOT / "state/job_funnel.sqlite3")
    parser.add_argument("--output", type=Path, default=ROOT / "public-case/evidence.json")
    args = parser.parse_args()
    payload = build(args.db)
    validate(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temp = args.output.with_suffix(args.output.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(args.output)
    print(json.dumps({"output": str(args.output), "integrity": payload["database_integrity"], "application_receipts": len(payload["verified_application_receipts"]), "email_receipts": payload["verified_email_receipt_count"], "ambiguous_intents": payload["ambiguous_intents"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
