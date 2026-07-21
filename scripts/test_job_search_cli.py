import json
import sqlite3
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "job_search_cli.py"


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
