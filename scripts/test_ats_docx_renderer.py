from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module():
    path = ROOT / "scripts" / "ats_docx_renderer.py"
    spec = importlib.util.spec_from_file_location("ats_docx_renderer_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_public_fixture_round_trips_through_ats_safe_docx(tmp_path):
    mod = load_module()
    package = {
        "resume": {
            "target_title": "Product Operations Lead",
            "summary": "Evidence-backed operator focused on reliable product delivery.",
            "evidence_bullets": ["Reduced manual handoffs through documented automation."],
            "ats_keywords": ["product operations", "automation"],
        },
        "provenance": {"claims": [{"text": "Reduced manual handoffs through documented automation."}]},
    }
    profile = {
        "candidate": {"public_name_ru": "Публичный кандидат", "english_level": "B2", "links": {}},
        "experience_highlights": [{
            "role": "Product Operations Lead", "company": "Example Company", "period": "2024–2026",
            "highlights_ru": ["Reduced manual handoffs through documented automation."],
        }],
        "education": {"items": [{"institution": "Example University", "field": "Product Management"}]},
    }
    output = mod.render_docx(package, profile, tmp_path / "resume.docx")
    text = mod.extract_docx_text(output)
    assert package["resume"]["target_title"] in text
    assert all(claim["text"] in text for claim in package["provenance"]["claims"])
    assert "Опыт работы" in text
    assert "Образование" in text
