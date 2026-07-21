import json
import os
import sqlite3
from pathlib import Path

import pytest

from portable_runtime import RuntimePaths, bootstrap_runtime, load_config
from local_funnel import LocalFunnel


def test_runtime_paths_use_explicit_home_without_machine_specific_paths(tmp_path):
    paths = RuntimePaths.from_home(tmp_path / "operator-home")

    assert paths.home == (tmp_path / "operator-home").resolve()
    assert paths.config == paths.home / "config.json"
    assert paths.database == paths.home / "state" / "operator.sqlite3"
    assert paths.candidate_facts == paths.home / "candidate" / "facts.json"
    assert paths.hh_profile == paths.home / "profiles" / "hh"
    assert paths.linkedin_profile == paths.home / "profiles" / "linkedin"
    assert "/opt/data" not in json.dumps(paths.as_dict())


def test_runtime_paths_default_to_xdg_data_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("JOB_SEARCH_HOME", raising=False)

    paths = RuntimePaths.from_environment()

    assert paths.home == (tmp_path / "xdg" / "job-search-operator").resolve()


def test_bootstrap_creates_private_layout_and_idempotent_database(tmp_path):
    paths = RuntimePaths.from_home(tmp_path / "runtime")

    first = bootstrap_runtime(paths)
    second = bootstrap_runtime(paths)

    assert first["created"] is True
    assert second["created"] is False
    assert paths.config.exists()
    assert paths.database.exists()
    assert paths.candidate_facts.exists()
    assert paths.evidence_dir.is_dir()
    assert paths.logs_dir.is_dir()
    if os.name != "nt":
        assert paths.home.stat().st_mode & 0o777 == 0o700
        assert paths.config.stat().st_mode & 0o777 == 0o600
        assert paths.database.stat().st_mode & 0o777 == 0o600

    with sqlite3.connect(paths.database) as conn:
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"jobs", "action_intents", "application_receipts", "batch_runs"} <= tables


def test_bootstrapped_database_is_compatible_with_channel_funnel(tmp_path):
    paths = RuntimePaths.from_home(tmp_path / "runtime")
    bootstrap_runtime(paths)

    with LocalFunnel(paths.database) as funnel:
        run_id = funnel.begin_batch_run(channel="compatibility", max_actions=1, started_at="2026-01-01T00:00:00+00:00")
        funnel.finish_batch_run(run_id=run_id, state="completed", reason="portable", now="2026-01-01T00:00:00+00:00")

    with sqlite3.connect(paths.database) as conn:
        assert conn.execute("SELECT state FROM batch_runs WHERE id=?", (run_id,)).fetchone()[0] == "completed"


def test_bootstrap_default_config_is_safe_and_non_executing(tmp_path):
    paths = RuntimePaths.from_home(tmp_path / "runtime")
    bootstrap_runtime(paths)

    config = load_config(paths)

    assert config["execution"]["enabled"] is False
    assert config["execution"]["hh_cap"] == 1
    assert config["execution"]["linkedin_cap"] == 1
    assert config["execution"]["email_cap"] == 1
    assert config["channels"] == {
        "hh": {"enabled": False},
        "linkedin": {"enabled": False},
        "ats": {"enabled": False},
        "gmail": {"enabled": False},
    }
