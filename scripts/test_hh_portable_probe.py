import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "hh_readonly_probe.js"


def test_hh_probe_has_no_machine_specific_dependency_or_paths():
    text = PROBE.read_text()
    assert "require('playwright')" in text
    assert "/tmp/pw" not in text
    assert "/opt/data" not in text
    assert "--runtime-home" in text
    assert "submit_attempted: false" in text


def test_hh_probe_rejects_non_hh_url_before_opening_browser(tmp_path):
    result = subprocess.run(
        [
            "node",
            str(PROBE),
            "--runtime-home",
            str(tmp_path),
            "--vacancy-url",
            "https://example.com/vacancy/1",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "hh.ru/hh.kz vacancy URL" in (result.stdout + result.stderr)


def test_hh_auth_flow_uses_official_browser_and_never_accepts_credentials():
    text = (ROOT / "scripts" / "hh_auth.js").read_text()
    assert "https://hh.ru/applicant/resumes" in text
    assert "launchPersistentContext" in text
    assert "password" not in text.lower()
    assert "process.stdin" not in text
    assert "submit" not in text.lower()
    assert "/opt/data" not in text
