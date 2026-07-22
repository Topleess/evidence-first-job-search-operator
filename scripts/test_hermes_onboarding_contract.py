import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_agents_instructions_define_safe_hermes_onboarding_contract():
    text = (ROOT / "AGENTS.md").read_text()

    for required in (
        "./job-search install",
        "./job-search doctor",
        "./job-search demo",
        "./job-search onboard --from-file",
        "Never ask for or store account passwords",
        "CAPTCHA",
        "OTP",
        "execution.enabled",
        "existing local browser profile, OAuth state, config, or credential helper",
        "least-privileged read-only health check",
        "Never commit, log, summarize, or send cookies/tokens",
    ):
        assert required in text


def test_public_onboarding_template_contains_no_identity_and_all_required_sections():
    path = ROOT / "config" / "onboarding.example.json"
    payload = json.loads(path.read_text())

    assert payload["candidate"]["display_name"] == "Your Name"
    assert payload["candidate"]["work_authorization"] == []
    assert payload["search"]["target_roles"] == []
    assert payload["search"]["excluded_roles"]
    assert payload["approved_facts"] == []
    assert "@" not in path.read_text()
