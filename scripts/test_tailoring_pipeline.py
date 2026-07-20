from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_module():
    path = ROOT / "scripts" / "tailoring_pipeline.py"
    spec = importlib.util.spec_from_file_location("tailoring_pipeline_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def profile():
    return {
        "candidate": {"public_name_ru": "Александр", "positioning_short_ru": "Product Manager с техническим бэкграундом.", "links": {"portfolio_primary": "https://example.dev"}},
        "target_roles": [{"role_family": "AI / Automation Product Manager", "keywords": ["AI Product Manager", "LLM", "RAG", "automation"], "cv_angle_ru": "AI-продукты от гипотезы до внедрения."}],
        "proof_points": [
            {"label": "MVP", "evidence_ru": "14 MVP за 48–72 часа.", "best_for_roles": ["AI / Automation Product Manager"], "privacy": "generally_usable"},
            {"label": "Private revenue", "evidence_ru": "Revenue 99 млн ₽.", "best_for_roles": ["AI / Automation Product Manager"], "privacy": "review_numbers_before_external_use"},
        ],
        "skills_keywords": {"technical": ["LLM", "RAG", "SQL"], "product": ["Roadmap", "MVP"]},
    }


def test_tailoring_uses_only_sourced_approved_claims_and_vacancy_terms():
    mod = load_module()
    vacancy = {
        "id": "v-1", "title": "AI Product Manager", "company": "Example",
        "description": "Ищем AI Product Manager: LLM, RAG, roadmap и запуск MVP.",
        "source_url": "https://jobs.example/v-1",
    }
    package = mod.build_tailored_package(profile(), vacancy)
    assert "14 MVP за 48–72 часа." in package["resume"]["evidence_bullets"]
    assert all("99 млн" not in text for text in package["resume"]["evidence_bullets"])
    assert set(package["resume"]["ats_keywords"]) == {"AI Product Manager", "LLM", "RAG", "Roadmap", "MVP"}
    assert package["provenance"]["claims"][0]["source_path"] == "proof_points[0].evidence_ru"
    mod.validate_tailored_package(package, profile(), vacancy)
    package["resume"]["evidence_bullets"].append("Увеличил прибыль на 300%.")
    with pytest.raises(mod.ProvenanceViolation):
        mod.validate_tailored_package(package, profile(), vacancy)


def test_hash_bound_manifest_authorizes_review_claim_and_tamper_blocks(tmp_path):
    mod = load_module()
    source = tmp_path / "baseline.txt"
    source.write_text("Revenue 99 млн ₽.")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = {
        "schema_version": "claim_approval_manifest.v1",
        "approvals": [{
            "claim_path": "proof_points[1].evidence_ru",
            "claim_text": "Revenue 99 млн ₽.",
            "approved_for_external_use": True,
            "source_file": "baseline.txt",
            "source_sha256": digest,
            "source_lines": [1, 1],
        }],
    }
    vacancy = {
        "id": "v-3", "title": "AI Product Manager", "company": "Example",
        "description": "Ищем AI Product Manager: LLM, RAG, roadmap и запуск MVP.",
        "source_url": "https://jobs.example/v-3",
    }
    package = mod.build_tailored_package(profile(), vacancy, manifest, tmp_path)
    assert "Revenue 99 млн ₽." in package["resume"]["evidence_bullets"]
    assert any(x["approval"] == "baseline_resume_manifest" for x in package["provenance"]["claims"])
    mod.validate_tailored_package(package, profile(), vacancy, manifest, tmp_path)
    source.write_text("tampered")
    with pytest.raises(mod.ProvenanceViolation, match="hash mismatch"):
        mod.build_tailored_package(profile(), vacancy, manifest, tmp_path)


def test_tailoring_blocks_thin_vacancy_facts():
    mod = load_module()
    with pytest.raises(mod.VacancyEvidenceMissing):
        mod.build_tailored_package(profile(), {"id": "v-2", "title": "PM", "company": "X", "description": "", "source_url": "https://jobs.example/v-2"})
