#!/usr/bin/env python3
"""Import the newest non-empty collector artifacts into local SQLite."""
from __future__ import annotations

import argparse
import glob
import json
from pathlib import Path
from typing import Any

from local_funnel import DEFAULT_DB, LocalFunnel, rows_from_payload

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATTERNS = {
    "linkedin": str(ROOT / "data/linkedin/public/normalized/vacancies_inbox_*.json"),
    "telegram": str(ROOT / "data/telegram/normalized/vacancies_inbox_*.json"),
    "ats": str(ROOT / "data/ats/ats_rows_*.json"),
    "job_boards": str(ROOT / "data/job_boards/vacancies_*.json"),
    "hh": str(ROOT / "data/hh/browser/vacancies_*.json"),
}


def latest_nonempty(pattern: str) -> Path | None:
    paths = sorted((Path(p) for p in glob.glob(pattern)), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if rows_from_payload(payload):
            return path
    return None


def sync(*, db: str | Path = DEFAULT_DB, patterns: dict[str, str] | None = None) -> dict[str, Any]:
    selected = patterns or DEFAULT_PATTERNS
    source_reports: dict[str, Any] = {}
    degraded = False
    with LocalFunnel(db) as funnel:
        for name, pattern in selected.items():
            try:
                path = latest_nonempty(pattern)
                if path is None:
                    source_reports[name] = {"status": "skipped", "reason": "no_nonempty_artifact"}
                    degraded = True
                    continue
                payload = json.loads(path.read_text(encoding="utf-8"))
                result = funnel.import_rows(rows_from_payload(payload))
                source_reports[name] = {"status": "success", "path": str(path), **result}
                if result["rejected"]:
                    source_reports[name]["status"] = "degraded"
                    degraded = True
            except Exception as exc:
                source_reports[name] = {"status": "error", "error": type(exc).__name__}
                degraded = True
        summary = funnel.summary()
    return {"status": "degraded" if degraded else "success", "sources": source_reports, "summary": summary}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DEFAULT_DB))
    args = parser.parse_args()
    report = sync(db=args.db)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
