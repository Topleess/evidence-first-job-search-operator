import importlib.util
import json
import sqlite3
from pathlib import Path

MODULE_PATH = Path(__file__).with_name("hh_supported_route.py")


def load_module():
    spec = importlib.util.spec_from_file_location("hh_supported_route", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_classifies_authentication_without_treating_login_cta_as_logout():
    mod = load_module()
    assert mod.classify_auth("https://hh.ru/applicant/resumes", "Мои резюме\nОтклики и приглашения") == "authenticated"
    assert mod.classify_auth("https://hh.ru/account/login", "Войти\nВведите телефон") == "login_required"
    assert mod.classify_auth("https://hh.ru/vacancy/1", "Войти\nProduct Manager") == "unknown"


def test_classifies_live_closed_and_already_applied_read_back():
    mod = load_module()
    assert mod.classify_vacancy("Product Manager\nОткликнуться") == "available"
    assert mod.classify_vacancy("Вакансия закрыта") == "closed"
    assert mod.classify_vacancy("Вы откликнулись 15 июля") == "already_applied"
    assert mod.classify_vacancy("Product Manager") == "unknown"


def test_durable_intent_is_idempotent_and_never_claims_submission(tmp_path):
    mod = load_module()
    db = tmp_path / "agent-hh.sqlite3"
    request = {"vacancy_id": "123", "url": "https://hh.ru/vacancy/123", "resume_id": "r1", "cover_sha256": "abc"}
    kz_request = {"vacancy_id": "124", "url": "https://hh.kz/vacancy/124", "resume_id": "r1", "cover_sha256": "abc"}
    with mod.HHState(db) as state:
        first = state.create_intent(request)
        second = state.create_intent(request)
        kz = state.create_intent(kz_request)
        assert first["intent_id"] == second["intent_id"]
        assert kz["intent_id"].startswith("hh:")
        assert first["status"] == second["status"] == "prepared"
        assert state.summary() == {"intents": 2, "receipts": 0}
    con = sqlite3.connect(db)
    assert con.execute("SELECT submitted FROM hh_application_intents").fetchone()[0] == 0


def test_observed_already_applied_creates_verified_read_only_receipt(tmp_path):
    mod = load_module()
    db = tmp_path / "agent-hh.sqlite3"
    with mod.HHState(db) as state:
        result = state.record_observation({
            "vacancy_id": "456",
            "url": "https://hh.kz/vacancy/456",
            "vacancy_status": "already_applied",
            "final_url": "https://hh.kz/vacancy/456",
            "evidence_sha256": "feed",
        })
        assert result["read_back_verified"] is True
        assert result["submitted_by_adapter"] is False
        assert state.summary() == {"intents": 0, "receipts": 1}


def test_rejects_production_database_path():
    mod = load_module()
    try:
        mod.HHState(Path("/opt/data/job-search/state/job_funnel.sqlite3"))
    except ValueError as exc:
        assert "production" in str(exc).lower()
    else:
        raise AssertionError("production DB path must be fenced")


def test_browser_probe_is_structurally_read_only():
    source = Path(__file__).with_name("hh_readonly_probe.js").read_text(encoding="utf-8")
    forbidden = [".click(", ".fill(", ".press(", ".check(", ".selectOption(", "keyboard.", "mouse."]
    assert not any(token in source for token in forbidden)
    assert "read_only: true" in source
