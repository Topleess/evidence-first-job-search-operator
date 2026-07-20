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


def test_real_tailored_package_round_trips_through_ats_safe_docx(tmp_path):
    mod = load_module()
    package = json.loads((ROOT / "state/tailored/0cddd744a044bf9f5a00.json").read_text())
    profile = json.loads((ROOT / "profile/candidate_profile.json").read_text())
    output = mod.render_docx(package, profile, tmp_path / "resume.docx")
    text = mod.extract_docx_text(output)
    assert package["resume"]["target_title"] in text
    assert all(claim["text"] in text for claim in package["provenance"]["claims"])
    assert "Опыт работы" in text
    assert "Образование" in text
