import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import job_search_cli
from portable_runtime import RuntimePaths, bootstrap_runtime


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "job_search_cli.py"
LAUNCHER = ROOT / "job-search"


def run_cli(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), "--home", str(home), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )


def parse_stdout(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.stdout.strip(), result.stderr
    return json.loads(result.stdout)


def test_install_creates_runtime_and_reports_safe_state(tmp_path):
    home = tmp_path / "operator"

    result = run_cli(home, "install")

    assert result.returncode == 0, result.stderr
    payload = parse_stdout(result)
    assert payload["command"] == "install"
    assert payload["created"] is True
    assert payload["execution_enabled"] is False
    assert (home / "state" / "operator.sqlite3").exists()


def test_repository_launcher_runs_without_installing_global_files(tmp_path):
    home = tmp_path / "operator"

    result = subprocess.run(
        [str(LAUNCHER), "--home", str(home), "install"],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert parse_stdout(result)["execution_enabled"] is False


def test_doctor_fails_before_install_and_passes_after_install(tmp_path):
    home = tmp_path / "operator"

    before = run_cli(home, "doctor")
    assert before.returncode == 1
    assert parse_stdout(before)["healthy"] is False

    assert run_cli(home, "install").returncode == 0
    after = run_cli(home, "doctor")

    assert after.returncode == 0, after.stderr
    payload = parse_stdout(after)
    assert payload["healthy"] is True
    assert payload["checks"]["database_integrity"] == "ok"
    assert payload["checks"]["execution_disabled"] is True
    assert payload["checks"]["candidate_facts_present"] is True


def test_demo_proves_exactly_once_and_second_run_no_duplicate(tmp_path):
    home = tmp_path / "operator"
    assert run_cli(home, "install").returncode == 0

    first = run_cli(home, "demo")
    second = run_cli(home, "demo")

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    first_payload = parse_stdout(first)
    second_payload = parse_stdout(second)
    assert first_payload["intent_created"] is True
    assert first_payload["side_effects"] == 1
    assert first_payload["verified_receipts"] == 1
    assert second_payload["intent_created"] is False
    assert second_payload["side_effects"] == 0
    assert second_payload["verified_receipts"] == 1
    assert second_payload["no_duplicate"] is True

    with sqlite3.connect(home / "state" / "operator.sqlite3") as conn:
        assert conn.execute("SELECT COUNT(*) FROM action_intents").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM application_receipts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM batch_runs").fetchone()[0] == 2


def test_demo_never_enables_real_execution(tmp_path):
    home = tmp_path / "operator"
    assert run_cli(home, "install").returncode == 0

    result = run_cli(home, "demo")

    payload = parse_stdout(result)
    assert payload["provider"] == "simulated-local"
    config = json.loads((home / "config.json").read_text())
    assert config["execution"]["enabled"] is False


def valid_onboarding_payload() -> dict:
    return {
        "candidate": {
            "display_name": "Demo Candidate",
            "location": "Berlin, Germany",
            "work_authorization": ["Germany"],
            "relocation": "open",
            "languages": [{"name": "English", "level": "C1"}],
        },
        "search": {
            "target_roles": ["AI Product Manager", "Product Operations Lead"],
            "excluded_roles": ["Developer", "Designer", "Support"],
            "locations": ["Remote", "Germany"],
            "salary_floor": {"amount": 5000, "currency": "EUR", "period": "month", "net": False},
        },
        "approved_facts": [
            {"id": "experience-product", "statement": "Led product discovery", "approved": True}
        ],
    }


def test_onboard_imports_confirmed_profile_without_enabling_execution(tmp_path):
    home = tmp_path / "operator"
    source = tmp_path / "onboarding.json"
    source.write_text(json.dumps(valid_onboarding_payload()))
    assert run_cli(home, "install").returncode == 0

    result = run_cli(home, "onboard", "--from-file", str(source))

    assert result.returncode == 0, result.stderr
    payload = parse_stdout(result)
    assert payload["profile_complete"] is True
    assert payload["approved_facts"] == 1
    saved = json.loads((home / "candidate" / "facts.json").read_text())
    assert saved["candidate"]["display_name"] == "Demo Candidate"
    assert saved["search"]["target_roles"] == ["AI Product Manager", "Product Operations Lead"]
    config = json.loads((home / "config.json").read_text())
    assert config["execution"]["enabled"] is False


def test_onboard_rejects_unapproved_fact_and_preserves_existing_profile(tmp_path):
    home = tmp_path / "operator"
    source = tmp_path / "onboarding.json"
    payload = valid_onboarding_payload()
    payload["approved_facts"][0]["approved"] = False
    source.write_text(json.dumps(payload))
    assert run_cli(home, "install").returncode == 0
    original = (home / "candidate" / "facts.json").read_text()

    result = run_cli(home, "onboard", "--from-file", str(source))

    assert result.returncode == 1
    assert parse_stdout(result)["error"] == "unapproved_candidate_fact"
    assert (home / "candidate" / "facts.json").read_text() == original


def test_status_reports_profile_and_channel_blockers(tmp_path):
    home = tmp_path / "operator"
    assert run_cli(home, "install").returncode == 0

    before = parse_stdout(run_cli(home, "status"))
    assert before["ready_for_demo"] is True
    assert before["ready_for_read_only"] is False
    assert "candidate_profile_incomplete" in before["blockers"]
    assert "no_channel_connected" in before["blockers"]
    assert before["ready_for_execute"] is False

    source = tmp_path / "onboarding.json"
    source.write_text(json.dumps(valid_onboarding_payload()))
    assert run_cli(home, "onboard", "--from-file", str(source)).returncode == 0
    after = parse_stdout(run_cli(home, "status"))
    assert "candidate_profile_incomplete" not in after["blockers"]
    assert "no_channel_connected" in after["blockers"]
    assert after["ready_for_execute"] is False


def test_successful_hh_authorization_marks_only_read_only_channel_ready(tmp_path, monkeypatch):
    paths = RuntimePaths.from_home(tmp_path / "operator")
    bootstrap_runtime(paths)
    monkeypatch.setattr(job_search_cli, "_run_hh_script", lambda *_args, **_kwargs: 0)

    assert job_search_cli.hh_auth(paths, headless=True) == 0

    config = json.loads(paths.config.read_text())
    assert config["channels"]["hh"]["enabled"] is True
    assert config["channels"]["hh"]["authorization"] == "verified"
    assert config["execution"]["enabled"] is False
