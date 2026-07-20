#!/usr/bin/env python3
"""Deterministic, provenance-first vacancy tailoring core."""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any


class ProvenanceViolation(RuntimeError):
    pass


class VacancyEvidenceMissing(ValueError):
    pass


def _norm(value: object) -> str:
    return " ".join(re.findall(r"[a-zа-яё0-9+#.]+", str(value).casefold()))


def _role(profile: dict[str, Any], vacancy_text: str) -> tuple[int, dict[str, Any]]:
    roles = profile.get("target_roles") or []
    if not roles:
        raise ProvenanceViolation("candidate profile has no target roles")
    text = _norm(vacancy_text)
    scored = []
    for index, role in enumerate(roles):
        terms = [role.get("role_family", ""), *(role.get("keywords") or [])]
        score = sum(1 for term in terms if _norm(term) and _norm(term) in text)
        scored.append((score, -index, index, role))
    _, _, index, role = max(scored)
    return index, role


def _ats_keywords(profile: dict[str, Any], role: dict[str, Any], vacancy_text: str) -> list[str]:
    candidates = list(role.get("keywords") or [])
    for values in (profile.get("skills_keywords") or {}).values():
        candidates.extend(values or [])
    text = _norm(vacancy_text)
    result: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        normalized = _norm(value)
        if normalized and normalized in text and normalized not in seen:
            seen.add(normalized)
            result.append(str(value))
    return result


def _approved_claims(
    manifest: dict[str, Any] | None, source_root: str | Path | None
) -> dict[str, dict[str, Any]]:
    if manifest is None:
        return {}
    if manifest.get("schema_version") != "claim_approval_manifest.v1" or source_root is None:
        raise ProvenanceViolation("invalid claim approval manifest or missing source root")
    root = Path(source_root).resolve()
    result: dict[str, dict[str, Any]] = {}
    for item in manifest.get("approvals") or []:
        if item.get("approved_for_external_use") is not True:
            continue
        relative = Path(str(item.get("source_file") or ""))
        source = (root / relative).resolve()
        if root not in source.parents or not source.is_file():
            raise ProvenanceViolation("approval source is missing or escapes source root")
        actual_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        if actual_hash != item.get("source_sha256"):
            raise ProvenanceViolation(f"approval source hash mismatch: {relative}")
        path = str(item.get("claim_path") or "")
        text = str(item.get("claim_text") or "").strip()
        if not path or not text or path in result:
            raise ProvenanceViolation("invalid or duplicate claim approval")
        result[path] = item
    return result


def build_tailored_package(
    profile: dict[str, Any],
    vacancy: dict[str, Any],
    approval_manifest: dict[str, Any] | None = None,
    source_root: str | Path | None = None,
) -> dict[str, Any]:
    description = str(vacancy.get("description") or "").strip()
    source_url = str(vacancy.get("source_url") or "").strip()
    title = str(vacancy.get("title") or "").strip()
    company = str(vacancy.get("company") or "").strip()
    if len(description) < 40 or not title or not company or not source_url.startswith(("http://", "https://")):
        raise VacancyEvidenceMissing("vacancy needs title, company, source URL and substantive description")
    vacancy_text = f"{title}\n{description}"
    role_index, role = _role(profile, vacancy_text)
    role_family = str(role.get("role_family") or "")
    approved_manifest = _approved_claims(approval_manifest, source_root)
    approved: list[tuple[int, dict[str, Any], str]] = []
    for index, point in enumerate(profile.get("proof_points") or []):
        path = f"proof_points[{index}].evidence_ru"
        manifest_item = approved_manifest.get(path)
        privacy_approval = point.get("privacy") == "generally_usable"
        manifest_approval = bool(
            manifest_item and str(manifest_item.get("claim_text") or "").strip() == str(point.get("evidence_ru") or "").strip()
        )
        if not privacy_approval and not manifest_approval:
            continue
        best = point.get("best_for_roles") or []
        if role_family in best:
            approval = "generally_usable" if privacy_approval else "baseline_resume_manifest"
            approved.append((index, point, approval))
    approved = approved[:3]
    bullets = [str(point["evidence_ru"]).strip() for _, point, _ in approved]
    candidate = profile.get("candidate") or {}
    positioning = str(candidate.get("positioning_short_ru") or "").strip()
    if not positioning:
        raise ProvenanceViolation("candidate positioning is missing")
    keywords = _ats_keywords(profile, role, vacancy_text)
    portfolio = str((candidate.get("links") or {}).get("portfolio_primary") or "").strip()
    evidence_sentence = bullets[0] if bullets else ""
    cover_parts = [
        f"Здравствуйте! Рассматриваю позицию {title} в {company}.",
        f"Мой релевантный фокус: {role.get('cv_angle_ru', positioning)}",
    ]
    if evidence_sentence:
        cover_parts.append(f"Подтверждённый пример: {evidence_sentence}")
    if portfolio:
        cover_parts.append(f"Кейсы: {portfolio}")
    claims = [
        {"text": text, "source_path": f"proof_points[{index}].evidence_ru", "approval": approval}
        for index, point, approval in approved
        for text in [str(point["evidence_ru"]).strip()]
    ]
    return {
        "schema_version": "tailored_package.v1",
        "vacancy": {"id": vacancy.get("id"), "title": title, "company": company, "source_url": source_url},
        "resume": {
            "target_title": title,
            "summary": positioning,
            "role_family": role_family,
            "ats_keywords": keywords,
            "evidence_bullets": bullets,
        },
        "cover_letter": "\n\n".join(cover_parts),
        "provenance": {
            "role_source_path": f"target_roles[{role_index}]",
            "vacancy_source_url": source_url,
            "claims": claims,
        },
    }


def validate_tailored_package(
    package: dict[str, Any],
    profile: dict[str, Any],
    vacancy: dict[str, Any],
    approval_manifest: dict[str, Any] | None = None,
    source_root: str | Path | None = None,
) -> None:
    expected_url = str(vacancy.get("source_url") or "")
    if package.get("provenance", {}).get("vacancy_source_url") != expected_url:
        raise ProvenanceViolation("vacancy source URL mismatch")
    manifest_approvals = _approved_claims(approval_manifest, source_root)
    allowed: dict[str, str] = {}
    for index, point in enumerate(profile.get("proof_points") or []):
        path = f"proof_points[{index}].evidence_ru"
        text = str(point.get("evidence_ru") or "").strip()
        item = manifest_approvals.get(path)
        if point.get("privacy") == "generally_usable" or (
            item and str(item.get("claim_text") or "").strip() == text
        ):
            allowed[text] = path
    bullets = package.get("resume", {}).get("evidence_bullets") or []
    claims = package.get("provenance", {}).get("claims") or []
    claim_map = {item.get("text"): item.get("source_path") for item in claims}
    for bullet in bullets:
        if bullet not in allowed or claim_map.get(bullet) != allowed[bullet]:
            raise ProvenanceViolation(f"unsourced or unapproved claim: {bullet}")
    vacancy_text = _norm(f"{vacancy.get('title', '')} {vacancy.get('description', '')}")
    for keyword in package.get("resume", {}).get("ats_keywords") or []:
        if _norm(keyword) not in vacancy_text:
            raise ProvenanceViolation(f"ATS keyword absent from vacancy: {keyword}")
