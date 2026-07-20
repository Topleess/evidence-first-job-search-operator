#!/usr/bin/env python3
"""Prepare a fingerprint-locked durable ATS application intent; never submits."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ats_form_classifier import plan_form


class ATSPreparationBlocked(RuntimeError):
    pass


def _canonical_fields(fields: list[dict[str, Any]]) -> bytes:
    reduced = [
        {
            "label": f.get("label", ""),
            "type": f.get("type", ""),
            "required": bool(f.get("required", False)),
            "options": f.get("options") or [],
        }
        for f in fields
    ]
    return json.dumps(reduced, ensure_ascii=False, separators=(",", ":")).encode()


def sha256_file(path: str | Path) -> str:
    candidate = Path(path).resolve()
    if not candidate.is_file():
        raise ATSPreparationBlocked(f"artifact missing: {candidate}")
    return hashlib.sha256(candidate.read_bytes()).hexdigest()


def prepare_ats_intent(
    *, funnel: Any, run_id: str, snapshot: dict[str, Any], answer_map: dict[str, Any],
    source: str, external_id: str, job_url: str, company: str, job_title: str,
    resume_path: str | Path, package_path: str | Path, now: str,
):
    if snapshot.get("schema_version") != "ats_form_snapshot.v1":
        raise ATSPreparationBlocked("unsupported snapshot schema")
    fields = snapshot.get("fields")
    if not isinstance(fields, list) or not fields:
        raise ATSPreparationBlocked("empty form snapshot")
    observed = hashlib.sha256(_canonical_fields(fields)).hexdigest()
    if observed != snapshot.get("form_fingerprint"):
        raise ATSPreparationBlocked("form snapshot fingerprint mismatch")
    if snapshot.get("source_url") != job_url and snapshot.get("source_url") != job_url.rstrip("/") + "/application":
        raise ATSPreparationBlocked("snapshot URL does not match vacancy")
    plan = plan_form(fields, answer_map)
    if plan["status"] != "ready":
        labels = [item["label"] for item in plan["blocked_fields"]]
        raise ATSPreparationBlocked("required form blockers: " + " | ".join(labels))
    resume = str(Path(resume_path).resolve())
    package = str(Path(package_path).resolve())
    payload = {
        "source": source, "external_id": str(external_id), "job_url": job_url,
        "company": company, "job_title": job_title,
        "form_fingerprint": observed,
        "resume_path": resume, "resume_sha256": sha256_file(resume),
        "package_path": package, "package_sha256": sha256_file(package),
        "answers": answer_map,
        "planned_fields": plan["actions"],
    }
    return funnel.reserve_action_intent(
        run_id=run_id, kind="application_submit",
        idempotency_key=f"{source}:{external_id}:application",
        payload=payload, now=now,
    )
