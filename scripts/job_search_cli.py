#!/usr/bin/env python3
"""Portable CLI for installing, diagnosing and demonstrating the operator."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
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
            "INSERT INTO batch_runs(id, channel, state, max_actions, started_at, heartbeat_at, detail) "
            "VALUES (?, ?, 'running', 1, ?, ?, '')",
            (run_id, channel, now, now),
        )
        row = conn.execute(
            "SELECT id FROM action_intents WHERE kind='demo_apply' AND idempotency_key=?",
            (external_id,),
        ).fetchone()
        if row is None:
            cursor = conn.execute(
                "INSERT INTO action_intents(run_id, kind, idempotency_key, payload, state, created_at, updated_at) "
                "VALUES (?, 'demo_apply', ?, ?, 'verified', ?, ?)",
                (
                    run_id,
                    external_id,
                    json.dumps({"synthetic": True, "contains_pii": False}),
                    now,
                    now,
                ),
            )
            intent_id = int(cursor.lastrowid)
            intent_created = True
            # This is the only demo side effect: a local deterministic receipt.
            provider_id = "demo-" + hashlib.sha256(external_id.encode()).hexdigest()[:16]
            conn.execute(
                "INSERT INTO application_receipts(job_url, external_vacancy_id, source, company, "
                "job_title, channel, status, submitted, read_back_verified, submitted_at, "
                "evidence_path, updated_at, run_id, action_intent_id) "
                "VALUES (?, ?, ?, 'Synthetic Company', 'Synthetic Product Manager', 'demo', "
                "'submitted', 1, 1, ?, ?, ?, ?, ?)",
                (
                    "demo://synthetic-product-manager-001",
                    external_id,
                    provider,
                    now,
                    provider_id,
                    now,
                    run_id,
                    intent_id,
                ),
            )
            side_effects = 1
        verified_receipts = conn.execute(
            "SELECT COUNT(*) FROM application_receipts "
            "WHERE source=? AND external_vacancy_id=? AND read_back_verified=1",
            (provider, external_id),
        ).fetchone()[0]
        finished = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE batch_runs SET state='completed', finished_at=?, heartbeat_at=?, stop_reason=?, detail=? WHERE id=?",
            (
                finished,
                finished,
                "demo_completed",
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


def _profile_complete(payload: dict) -> bool:
    candidate = payload.get("candidate", {})
    search = payload.get("search", {})
    required_candidate = ("display_name", "location", "work_authorization", "relocation", "languages")
    required_search = ("target_roles", "excluded_roles", "locations", "salary_floor")
    return (
        all(candidate.get(key) not in (None, "", []) for key in required_candidate)
        and all(search.get(key) not in (None, "", []) for key in required_search)
        and bool(payload.get("approved_facts"))
    )


def onboard(paths: RuntimePaths, source: Path) -> int:
    if doctor_payload(paths) is not None:
        emit({"command": "onboard", "error": "runtime_not_healthy"})
        return 1
    try:
        payload = json.loads(source.read_text())
    except (OSError, json.JSONDecodeError):
        emit({"command": "onboard", "error": "invalid_onboarding_file"})
        return 1
    facts = payload.get("approved_facts", [])
    if any(fact.get("approved") is not True for fact in facts):
        emit({"command": "onboard", "error": "unapproved_candidate_fact"})
        return 1
    if not _profile_complete(payload):
        emit({"command": "onboard", "error": "incomplete_candidate_profile"})
        return 1
    saved = {
        "schema_version": "candidate_facts.v1",
        "candidate": payload["candidate"],
        "search": payload["search"],
        "approved_facts": facts,
    }
    temporary = paths.candidate_facts.with_suffix(".tmp")
    temporary.write_text(json.dumps(saved, ensure_ascii=False, indent=2) + "\n")
    if os.name != "nt":
        temporary.chmod(0o600)
    temporary.replace(paths.candidate_facts)
    emit(
        {
            "command": "onboard",
            "profile_complete": True,
            "approved_facts": len(facts),
            "execution_enabled": False,
        }
    )
    return 0


def status(paths: RuntimePaths) -> int:
    health_error = doctor_payload(paths)
    if health_error is not None:
        emit({"command": "status", "healthy": False, "blockers": [health_error]})
        return 1
    config = load_config(paths)
    try:
        candidate = json.loads(paths.candidate_facts.read_text())
    except json.JSONDecodeError:
        candidate = {}
    blockers: list[str] = []
    if not _profile_complete(candidate):
        blockers.append("candidate_profile_incomplete")
    connected = [
        name for name, settings in config["channels"].items() if settings.get("enabled") is True
    ]
    if not connected:
        blockers.append("no_channel_connected")
    execution_enabled = bool(config["execution"]["enabled"])
    emit(
        {
            "command": "status",
            "healthy": True,
            "ready_for_demo": True,
            "ready_for_read_only": not blockers,
            "ready_for_execute": not blockers and execution_enabled,
            "execution_enabled": execution_enabled,
            "connected_channels": connected,
            "blockers": blockers,
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="job-search")
    parser.add_argument("--home", help="Per-user runtime home")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("install", help="Create a private safe runtime")
    sub.add_parser("doctor", help="Verify runtime health")
    sub.add_parser("demo", help="Run a synthetic exactly-once lifecycle")
    onboard_parser = sub.add_parser("onboard", help="Import a human-confirmed candidate profile")
    onboard_parser.add_argument("--from-file", required=True, type=Path)
    sub.add_parser("status", help="Show readiness and exact blockers")
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
    if args.command == "onboard":
        return onboard(paths, args.from_file)
    if args.command == "status":
        return status(paths)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
