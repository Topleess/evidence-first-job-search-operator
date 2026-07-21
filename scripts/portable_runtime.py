#!/usr/bin/env python3
"""Portable per-user runtime layout for Job Search Operator."""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": "job_search_operator_config.v1",
    "execution": {
        "enabled": False,
        "hh_cap": 1,
        "linkedin_cap": 1,
        "email_cap": 1,
    },
    "channels": {
        "hh": {"enabled": False},
        "linkedin": {"enabled": False},
        "ats": {"enabled": False},
        "gmail": {"enabled": False},
    },
}


@dataclass(frozen=True)
class RuntimePaths:
    home: Path
    config: Path
    database: Path
    candidate_facts: Path
    hh_profile: Path
    linkedin_profile: Path
    evidence_dir: Path
    logs_dir: Path

    @classmethod
    def from_home(cls, home: str | Path) -> "RuntimePaths":
        root = Path(home).expanduser().resolve()
        return cls(
            home=root,
            config=root / "config.json",
            database=root / "state" / "operator.sqlite3",
            candidate_facts=root / "candidate" / "facts.json",
            hh_profile=root / "profiles" / "hh",
            linkedin_profile=root / "profiles" / "linkedin",
            evidence_dir=root / "evidence",
            logs_dir=root / "logs",
        )

    @classmethod
    def from_environment(cls) -> "RuntimePaths":
        explicit = os.environ.get("JOB_SEARCH_HOME")
        if explicit:
            return cls.from_home(explicit)
        xdg = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        return cls.from_home(xdg / "job-search-operator")

    def as_dict(self) -> dict[str, str]:
        return {name: str(value) for name, value in vars(self).items()}


def _write_private_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    if os.name != "nt":
        path.chmod(0o600)


def _initialize_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # LocalFunnel initializes the versioned public core schema and then adds
    # the channel-specific intent/receipt tables used by production workers.
    from local_funnel import LocalFunnel

    with LocalFunnel(path):
        pass
    if os.name != "nt":
        path.chmod(0o600)


def bootstrap_runtime(paths: RuntimePaths) -> dict[str, Any]:
    created = not paths.config.exists() and not paths.database.exists()
    for directory in (
        paths.home,
        paths.hh_profile,
        paths.linkedin_profile,
        paths.evidence_dir,
        paths.logs_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        paths.home.chmod(0o700)
    if not paths.config.exists():
        _write_private_json(paths.config, DEFAULT_CONFIG)
    if not paths.candidate_facts.exists():
        _write_private_json(
            paths.candidate_facts,
            {"schema_version": "candidate_facts.v1", "approved_facts": []},
        )
    _initialize_database(paths.database)
    return {"created": created, "paths": paths.as_dict()}


def load_config(paths: RuntimePaths) -> dict[str, Any]:
    return json.loads(paths.config.read_text())
