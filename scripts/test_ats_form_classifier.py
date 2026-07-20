from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_module():
    path = ROOT / "scripts" / "ats_form_classifier.py"
    spec = importlib.util.spec_from_file_location("ats_form_classifier_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_known_fields_ready_but_required_unknown_blocks_only_form():
    mod = load_module()
    fields = [
        {"id": "n", "label": "Full name", "required": True},
        {"id": "e", "label": "Email address", "required": True},
        {"id": "r", "label": "Resume / CV", "type": "file", "required": True},
        {"id": "c", "label": "Cover letter", "type": "textarea", "required": True},
        {"id": "v", "label": "Will you now or later require visa sponsorship?", "required": True},
        {"id": "x", "label": "Twitter profile", "required": False},
    ]
    answers = {"full_name": "Alexander", "email": "a@example.test", "resume": "/private/resume.docx", "cover_letter": "Sourced letter"}
    plan = mod.plan_form(fields, answers)
    assert plan["status"] == "blocked"
    assert [x["field_id"] for x in plan["actions"]] == ["n", "e", "r", "c"]
    assert plan["blocked_fields"] == [{"field_id": "v", "label": fields[4]["label"], "kind": "sensitive_unknown", "reason": "required_truthful_answer_missing"}]
    assert plan["optional_unanswered"][0]["field_id"] == "x"


def test_complete_truthful_map_is_ready_and_labels_not_uuid_bound():
    mod = load_module()
    fields = [
        {"id": "random-uuid-1", "label": "Имя и фамилия", "required": True},
        {"id": "random-uuid-2", "label": "Personal website", "required": True},
    ]
    plan = mod.plan_form(fields, {"full_name": "Alexander", "portfolio": "https://example.dev"})
    assert plan["status"] == "ready"
    assert [x["kind"] for x in plan["actions"]] == ["full_name", "portfolio"]


def test_field_id_answers_allow_distinct_required_custom_questions():
    mod = load_module()
    fields = [
        {"id": "motivation-id", "label": "Why us?", "required": True},
        {"id": "project-id", "label": "Describe a project", "required": True},
    ]
    plan = mod.plan_form(fields, {
        "motivation-id": "Vacancy-specific sourced motivation",
        "project-id": "Candidate-profile sourced project",
    })
    assert plan["status"] == "ready"
    assert [item["value"] for item in plan["actions"]] == [
        "Vacancy-specific sourced motivation",
        "Candidate-profile sourced project",
    ]


def test_choice_answers_must_match_visible_options_exactly():
    mod = load_module()
    fields = [{
        "id": "contract-location",
        "label": "Tick all options relevant for the main location",
        "type": "multichoice",
        "required": True,
        "options": ["Germany without visa support", "Germany with visa support"],
    }]
    invalid = mod.plan_form(fields, {"location": "Germany — relocation"})
    assert invalid["status"] == "blocked"
    assert invalid["blocked_fields"][0]["reason"] == "invalid_choice_answer"
    valid = mod.plan_form(fields, {"contract-location": ["Germany with visa support"]})
    assert valid["status"] == "ready"
    assert valid["actions"][0]["value"] == ["Germany with visa support"]
