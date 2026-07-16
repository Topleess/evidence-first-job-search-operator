#!/usr/bin/env python3
"""Silent Gmail outreach watchdog: stdout only for new replies or bounces."""
from __future__ import annotations

import json
from pathlib import Path

from gmail_api_tracking import DEFAULT_RECEIPTS, DEFAULT_REPORT, DEFAULT_TOKEN, select_unseen_events, sync

STATE = Path("/opt/data/job-search/state/gmail_outreach_notified.json")


def main() -> int:
    report = sync(token_path=DEFAULT_TOKEN, receipts_dir=DEFAULT_RECEIPTS)
    DEFAULT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    prior = json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {}
    events, next_state = select_unseen_events(report, prior)
    STATE.write_text(json.dumps(next_state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for row in events:
        label = "Ответ" if row["status"] == "reply" else "Ошибка доставки"
        print(f"{label}: {row.get('to', '')} · {row.get('subject', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
