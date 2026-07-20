#!/usr/bin/env python3
"""Adaptive ATS field classification with fail-closed truthful answers."""
from __future__ import annotations

import re
from typing import Any


class InvalidFormDescriptor(ValueError):
    pass


def _norm(text: object) -> str:
    return " ".join(re.findall(r"[a-zа-яё0-9]+", str(text).casefold()))


FIELD_PATTERNS: list[tuple[str, tuple[str, ...]]] = [
    ("full_name", ("full name", "your name", "name", "имя и фамилия", "фио")),
    ("email", ("email", "e mail", "электронная почта")),
    ("phone", ("phone", "telephone", "телефон")),
    ("location", ("location", "current location", "город проживания", "локация")),
    ("resume", ("resume", "cv", "резюме")),
    ("cover_letter", ("cover letter", "сопроводительное письмо")),
    ("linkedin", ("linkedin",)),
    ("portfolio", ("portfolio", "website", "personal site", "сайт", "портфолио")),
]
SENSITIVE_UNKNOWN = (
    "work authorization", "authorized to work", "visa sponsorship", "sponsorship", "sponsor an immigration case",
    "разрешение на работу", "спонсорство визы", "salary expectation", "expected salary", "expected yearly salary",
    "compensation", "зарплатные ожидания", "security clearance", "background check",
)


def classify_field(field: dict[str, Any]) -> dict[str, Any]:
    field_id = str(field.get("id") or "").strip()
    label = str(field.get("label") or "").strip()
    if not field_id or not label:
        raise InvalidFormDescriptor("every field needs stable id and visible label")
    text = _norm(label)
    if any(_norm(term) in text for term in SENSITIVE_UNKNOWN):
        kind = "sensitive_unknown"
    else:
        kind = "custom_unknown"
        for candidate_kind, patterns in FIELD_PATTERNS:
            if any(_norm(pattern) in text for pattern in patterns):
                kind = candidate_kind
                break
    return {**field, "id": field_id, "label": label, "kind": kind, "required": bool(field.get("required"))}


def plan_form(fields: list[dict[str, Any]], answer_map: dict[str, Any]) -> dict[str, Any]:
    actions, blocked, optional_unknown = [], [], []
    seen: set[str] = set()
    for raw in fields:
        field = classify_field(raw)
        if field["id"] in seen:
            raise InvalidFormDescriptor(f"duplicate field id: {field['id']}")
        seen.add(field["id"])
        value = answer_map.get(field["id"], answer_map.get(field["kind"]))
        options = field.get("options") or []
        invalid_choice = False
        if value not in (None, "", []):
            if field.get("type") == "multichoice":
                invalid_choice = not isinstance(value, list) or not value or any(item not in options for item in value)
            elif field.get("type") == "choice" and field["kind"] != "resume" and options:
                invalid_choice = not isinstance(value, str) or value not in options
        if value not in (None, "", []) and not invalid_choice:
            actions.append({"field_id": field["id"], "kind": field["kind"], "value": value, "type": field.get("type", "text")})
        elif field["required"] or invalid_choice:
            blocked.append({"field_id": field["id"], "label": field["label"], "kind": field["kind"], "reason": "invalid_choice_answer" if invalid_choice else "required_truthful_answer_missing"})
        else:
            optional_unknown.append({"field_id": field["id"], "label": field["label"], "kind": field["kind"]})
    return {
        "status": "blocked" if blocked else "ready",
        "actions": actions,
        "blocked_fields": blocked,
        "optional_unanswered": optional_unknown,
        "field_count": len(fields),
    }
