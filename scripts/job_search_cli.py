#!/usr/bin/env python3
"""Portable CLI for installing, diagnosing and demonstrating the operator."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from portable_runtime import RuntimePaths, bootstrap_runtime, load_config


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def install(paths: RuntimePaths) -> int:
    result = bootstrap_runtime(paths)
    config = load_config(paths)
    emit(
        {
            "command": "install",
            "created": result["created"],
            "execution_enabled": config["execution"]["enabled"],
            "home": str(paths.home),
        }
    )
    return 0


def doctor(paths: RuntimePaths) -> int:
    checks: dict[str, object] = {
        "config_present": paths.config.exists(),
        "database_present": paths.database.exists(),
        "candidate_facts_present": paths.candidate_facts.exists(),
    }
    if paths.config.exists():
        try:
            checks["execution_disabled"] = not bool(
                load_config(paths)["execution"]["enabled"]
            )
        except (KeyError, json.JSONDecodeError):
            checks["execution_disabled"] = False
    else:
        checks["execution_disabled"] = False
    if paths.database.exists():
        try:
            with sqlite3.connect(paths.database) as conn:
                checks["database_integrity"] = conn.execute(
                    "PRAGMA integrity_check"
                ).fetchone()[0]
        except sqlite3.Error:
            checks["database_integrity"] = "error"
    else:
        checks["database_integrity"] = "missing"
    healthy = all(
        (
            checks["config_present"],
            checks["database_present"],
            checks["candidate_facts_present"],
            checks["execution_disabled"],
            checks["database_integrity"] == "ok",
        )
    )
    emit({"command": "doctor", "healthy": healthy, "checks": checks})
    return 0 if healthy else 1


def demo(paths: RuntimePaths) -> int:
    if doctor_payload(paths) is not None:
        emit({"command": "demo", "error": "runtime_not_healthy"})
        return 1
    now = datetime.now(timezone.utc).isoformat()
    run_id = uuid.uuid4().hex
    channel = "demo"
    external_id = "synthetic-product-manager-001"
    provider = "simulated-local"
    intent_created = False
    side_effects = 0

    with sqlite3.connect(paths.database) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO batch_runs(id, channel, state, started_at, metadata) "
            "VALUES (?, ?, 'running', ?, ?)",
            (run_id, channel, now, json.dumps({"mode": "demo"})),
        )
        row = conn.execute(
            "SELECT id FROM action_intents WHERE channel=? AND external_id=?",
            (channel, external_id),
        ).fetchone()
        if row is None:
            cursor = conn.execute(
                "INSERT INTO action_intents(run_id, channel, external_id, state, payload) "
                "VALUES (?, ?, ?, 'prepared', ?)",
                (
                    run_id,
                    channel,
                    external_id,
                    json.dumps({"synthetic": True, "contains_pii": False}),
                ),
            )
            intent_id = int(cursor.lastrowid)
            intent_created = True
            # This is the only demo side effect: a local deterministic receipt.
            provider_id = "demo-" + hashlib.sha256(external_id.encode()).hexdigest()[:16]
            conn.execute(
                "UPDATE action_intents SET state='executed' WHERE id=?",
                (intent_id,),
            )
            conn.execute(
                "INSERT INTO application_receipts(intent_id, source, external_vacancy_id, "
                "submitted, read_back_verified, provider_receipt_id) VALUES (?, ?, ?, 1, 1, ?)",
                (intent_id, provider, external_id, provider_id),
            )
            side_effects = 1
        verified_receipts = conn.execute(
            "SELECT COUNT(*) FROM application_receipts "
            "WHERE source=? AND external_vacancy_id=? AND read_back_verified=1",
            (provider, external_id),
        ).fetchone()[0]
        conn.execute(
            "UPDATE batch_runs SET state='completed', finished_at=?, metadata=? WHERE id=?",
            (
                datetime.now(timezone.utc).isoformat(),
                json.dumps(
                    {
                        "intent_created": intent_created,
                        "side_effects": side_effects,
                        "verified_receipts": verified_receipts,
                    }
                ),
                run_id,
            ),
        )
    emit(
        {
            "command": "demo",
            "run_id": run_id,
            "provider": provider,
            "intent_created": intent_created,
            "side_effects": side_effects,
            "verified_receipts": verified_receipts,
            "no_duplicate": not intent_created and side_effects == 0,
        }
    )
    return 0


def doctor_payload(paths: RuntimePaths) -> str | None:
    if not paths.config.exists() or not paths.database.exists() or not paths.candidate_facts.exists():
        return "missing_runtime"
    try:
        if load_config(paths)["execution"]["enabled"]:
            return "execution_enabled"
        with sqlite3.connect(paths.database) as conn:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return "database_integrity"
    except (KeyError, json.JSONDecodeError, sqlite3.Error):
        return "invalid_runtime"
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="job-search")
    parser.add_argument("--home", help="Per-user runtime home")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("install", help="Create a private safe runtime")
    sub.add_parser("doctor", help="Verify runtime health")
    sub.add_parser("demo", help="Run a synthetic exactly-once lifecycle")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    paths = RuntimePaths.from_home(args.home) if args.home else RuntimePaths.from_environment()
    if args.command == "install":
        return install(paths)
    if args.command == "doctor":
        return doctor(paths)
    if args.command == "demo":
        return demo(paths)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
