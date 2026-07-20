#!/usr/bin/env python3
"""Durable HH submit state bridge for the browser executor."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from urllib.parse import urlparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from local_funnel import LocalFunnel


def _digest(value: str, name: str) -> str:
    value = str(value).strip().lower()
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError(f"{name} must be sha256 hex")
    return value


def reserve_hh(
    funnel: LocalFunnel, *, run_id: str, vacancy: dict[str, Any], form_fingerprint: str,
    truth_map_sha256: str, plan_sha256: str, now: str, daily_cap: int = 20,
):
    vacancy_id = str(vacancy["id"]).strip()
    job_url = str(vacancy["url"])
    parsed_url = urlparse(job_url)
    if (
        not vacancy_id.isdigit() or parsed_url.scheme != "https" or parsed_url.hostname not in {"hh.ru", "hh.kz"}
        or parsed_url.username or parsed_url.password or parsed_url.port or parsed_url.query or parsed_url.fragment
        or parsed_url.path != f"/vacancy/{vacancy_id}" or job_url != f"https://{parsed_url.hostname}/vacancy/{vacancy_id}"
    ):
        raise ValueError("non-canonical HH vacancy URL")
    if isinstance(daily_cap, bool) or not isinstance(daily_cap, int) or daily_cap < 1:
        raise ValueError("daily_cap must be a positive integer")
    payload = {
        "source": "hh",
        "external_id": vacancy_id,
        "job_url": job_url,
        "company": str(vacancy.get("company") or "Не указана"),
        "job_title": str(vacancy.get("title") or "Не указана"),
        "form_fingerprint": _digest(form_fingerprint, "form_fingerprint"),
        "truth_map_sha256": _digest(truth_map_sha256, "truth_map_sha256"),
        "plan_sha256": _digest(plan_sha256, "plan_sha256"),
        "daily_cap": daily_cap,
    }
    return funnel.reserve_action_intent(
        run_id=run_id, kind="application_submit", idempotency_key=f"hh:{vacancy_id}:application",
        payload=payload, now=now,
    )


def record_verified_hh(
    funnel: LocalFunnel, *, intent_id: int, execution_token: str = "", reconciliation_token: str = "",
    result: dict[str, Any], submitted_at: str,
) -> int:
    capability_token = execution_token or reconciliation_token
    payload = funnel.execution_intent_payload(
        intent_id=intent_id, execution_token=execution_token, reconciliation_token=reconciliation_token
    )
    vacancy_id = str(result.get("id") or "")
    job_url = str(result.get("url") or "")
    expected_url = str(payload.get("job_url") or "")
    evidence_path = Path(str(result.get("evidence_path") or ""))
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    parsed = urlparse(job_url)
    submitted = datetime.fromisoformat(str(submitted_at).replace("Z", "+00:00"))
    started = datetime.fromisoformat(str(payload.get("_execution_started_at") or "").replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    readback_text = str(evidence.get("readback_text") or "")
    computed_readback = hashlib.sha256(readback_text.encode("utf-8")).hexdigest()
    token_digest = hashlib.sha256(capability_token.encode("utf-8")).hexdigest()
    if (
        submitted.tzinfo is None or started.tzinfo is None or not vacancy_id.isdigit()
        or submitted < started or submitted > now or now - submitted > timedelta(minutes=30) or submitted - started > timedelta(minutes=20)
        or vacancy_id != str(payload.get("external_id") or "")
        or job_url != expected_url or parsed.scheme != "https" or parsed.hostname not in {"hh.ru", "hh.kz"}
        or parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment
        or parsed.path != f"/vacancy/{vacancy_id}"
        or evidence.get("marker") != "already_applied_on_reopen"
        or str(evidence.get("id") or "") != vacancy_id
        or evidence.get("url") != expected_url or evidence.get("final_url") != expected_url
        or evidence.get("observed_at") != submitted_at
        or not readback_text or _digest(evidence.get("readback_text_sha256") or "", "readback_text_sha256") != computed_readback
        or int(evidence.get("intent_id") or 0) != intent_id
        or _digest(evidence.get("execution_token_sha256") or "", "execution_token_sha256") != token_digest
        or str(result.get("company") or "") != str(payload.get("company") or "")
        or str(result.get("title") or "") != str(payload.get("job_title") or "")
        or _digest(evidence.get("form_fingerprint") or "", "form_fingerprint") != str(payload.get("form_fingerprint") or "")
        or _digest(evidence.get("truth_map_sha256") or "", "truth_map_sha256") != str(payload.get("truth_map_sha256") or "")
        or _digest(evidence.get("plan_sha256") or "", "plan_sha256") != str(payload.get("plan_sha256") or "")
    ):
        raise ValueError("verified HH evidence is not vacancy-bound")
    return funnel._record_application(
        source="hh", external_vacancy_id=vacancy_id, job_url=job_url,
        company=str(result.get("company") or "Не указана"), job_title=str(result.get("title") or "Не указана"),
        status="submitted", submitted_at=submitted.isoformat(), read_back_verified=True,
        evidence_path=str(evidence_path), channel="platform", intent_id=intent_id,
        execution_token=execution_token or None, reconciliation_token=reconciliation_token or None,
        strict_evidence_verified=True,
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    reserve = sub.add_parser("reserve"); reserve.add_argument("--input", required=True)
    begin = sub.add_parser("begin"); begin.add_argument("--intent-id", type=int, required=True); begin.add_argument("--worker-id", required=True)
    check = sub.add_parser("check"); check.add_argument("--intent-id", type=int, required=True); check.add_argument("--token", default=os.environ.get("HH_EXECUTION_TOKEN"), required=False)
    ambiguous = sub.add_parser("ambiguous"); ambiguous.add_argument("--intent-id", type=int, required=True); ambiguous.add_argument("--token", default=os.environ.get("HH_EXECUTION_TOKEN"), required=False); ambiguous.add_argument("--reason", required=True)
    receipt = sub.add_parser("receipt"); receipt.add_argument("--intent-id", type=int, required=True); receipt.add_argument("--token", default=os.environ.get("HH_EXECUTION_TOKEN"), required=False); receipt.add_argument("--result", required=True)
    reconcile_receipt = sub.add_parser("reconcile-receipt"); reconcile_receipt.add_argument("--intent-id", type=int, required=True); reconcile_receipt.add_argument("--token", default=os.environ.get("HH_RECONCILIATION_TOKEN"), required=False); reconcile_receipt.add_argument("--result", required=True)
    args = parser.parse_args()
    with LocalFunnel(args.db) as funnel:
        if args.command == "reserve":
            data = json.loads(Path(args.input).read_text(encoding="utf-8")); intent = reserve_hh(funnel, now=utc_now(), **data)
            output = {"intent_id": intent.intent_id, "created": intent.created}
        elif args.command == "begin":
            output = {"execution_token": funnel.mark_intent_executing(intent_id=args.intent_id, worker_id=args.worker_id, now=utc_now())}
        elif args.command == "check":
            funnel.assert_intent_execution_fence(intent_id=args.intent_id, execution_token=args.token)
            output = {"state": "execution_fence_valid"}
        elif args.command == "ambiguous":
            funnel.mark_intent_ambiguous(intent_id=args.intent_id, execution_token=args.token, now=utc_now(), error_code=args.reason[:160])
            output = {"state": "ambiguous"}
        elif args.command == "receipt":
            data = json.loads(Path(args.result).read_text(encoding="utf-8"))
            if not data.get("submitted_at"):
                raise ValueError("submitted_at is required")
            rid = record_verified_hh(funnel, intent_id=args.intent_id, execution_token=args.token, result=data, submitted_at=data["submitted_at"])
            print(json.dumps({"receipt_id": rid})); return 0
        else:
            data = json.loads(Path(args.result).read_text(encoding="utf-8"))
            if not data.get("submitted_at"):
                raise ValueError("submitted_at is required")
            rid = record_verified_hh(funnel, intent_id=args.intent_id, reconciliation_token=args.token, result=data, submitted_at=data["submitted_at"])
            print(json.dumps({"receipt_id": rid})); return 0
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
