#!/usr/bin/env python3
"""Conservatively attach fresh live HH eligibility evidence to queued jobs.

Read-only against HH. It never logs in or applies. Eligible rows are updated in-place
for hh_cron_runner; rejected rows are terminally failed so stale/unsafe items are not
revisited forever.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sqlite3
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

TARGET_RE = re.compile(r"\b(product|продакт|product owner|product lead|project lead|product operations|founder associate)\b", re.I)
EXCLUDED_TITLE_RE = re.compile(r"\b(developer|разработчик|designer|дизайнер|marketing|маркетолог|support|поддержк)\w*", re.I)
ARCHIVED_STATE_RE = re.compile(
    r'(?:["\']archived["\']\s*:\s*["\']true["\']|(?:is)?Archived(?:&#34;|["\'])\s*:\s*true)',
    re.I,
)
DESCRIPTION_RE = re.compile(r'data-qa=["\']vacancy-description["\']', re.I)
REMOTE_RE = re.compile(r"\b(remote|удал[её]нн|дистанцион)\w*", re.I)
HARD_EXCLUSION_RE = re.compile(
    r"(?:обязател\w*|must|required)[^.!?\n]{0,100}\b(?:java|python|javascript|typescript|c\+\+|golang|php|swift|kotlin)\b",
    re.I,
)
SALARY_NUMBER_RE = re.compile(r"(\d[\d\s]{2,})")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical_hh_url(url: str, vacancy_id: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    if host not in {"hh.ru", "www.hh.ru", "hh.kz", "www.hh.kz"} or not vacancy_id.isdigit():
        raise ValueError("invalid HH URL/id")
    suffix = "kz" if host.endswith("hh.kz") else "ru"
    return f"https://hh.{suffix}/vacancy/{vacancy_id}"


def plain_text(raw: str) -> str:
    raw = re.sub(r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>", " ", raw, flags=re.I | re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw))).strip()


def salary_floor(metadata: dict) -> int | None:
    value = str(metadata.get("salary") or "")
    nums = [int(x.replace(" ", "")) for x in SALARY_NUMBER_RE.findall(value)]
    return min(nums) if nums else None


def assess(payload: dict, job: sqlite3.Row, raw: str, final_url: str) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    title = str(payload.get("job_title") or job["title"] or "")
    location = str(job["location"] or "")
    metadata = json.loads(job["metadata"] or "{}")
    text = plain_text(raw)
    if ARCHIVED_STATE_RE.search(raw):
        reasons.append("vacancy_archived")
    if not DESCRIPTION_RE.search(raw):
        reasons.append("vacancy_description_missing")
    if not TARGET_RE.search(title):
        reasons.append("title_outside_target")
    if EXCLUDED_TITLE_RE.search(title):
        reasons.append("excluded_title_family")
    if not REMOTE_RE.search(location + " " + text[:12000]):
        reasons.append("remote_or_relocation_not_evidenced")
    floor = salary_floor(metadata)
    if floor is not None and floor < 130000:
        reasons.append("salary_floor_below_130k")
    if HARD_EXCLUSION_RE.search(text[:60000]):
        reasons.append("mandatory_engineering_language_detected")
    expected = str(payload.get("external_id") or "")
    if f"/vacancy/{expected}" not in final_url:
        reasons.append("final_url_id_mismatch")
    return not reasons, reasons


def fetch(url: str) -> tuple[str, str]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; JobSearchOperator/1.0)"})
    with opener.open(request, timeout=30) as response:
        return response.read().decode("utf-8", "replace"), response.geturl()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="state/job_funnel.sqlite3")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--evidence-dir", default="state/eligibility/hh")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.limit <= 0:
        raise SystemExit("--limit must be positive")
    evidence_dir = Path(args.evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(args.db, timeout=10)
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT q.id queue_id,q.payload,j.* FROM queue q
           JOIN jobs j ON j.source=json_extract(q.payload,'$.source')
             AND j.external_id=json_extract(q.payload,'$.external_id')
           WHERE q.state='pending' AND json_extract(q.payload,'$.source')='hh'
           ORDER BY CAST(json_extract(q.payload,'$.fit_score') AS INTEGER) DESC,q.id LIMIT ?""",
        (args.limit,),
    ).fetchall()
    report = {"checked": 0, "eligible": 0, "rejected": 0, "errors": []}
    for row in rows:
        payload = json.loads(row["payload"])
        vacancy_id = str(payload.get("external_id") or "")
        checked_at = now_iso()
        try:
            url = canonical_hh_url(str(payload.get("job_url") or row["url"]), vacancy_id)
            raw, final_url = fetch(url)
            eligible, reasons = assess(payload, row, raw, final_url)
            evidence = {
                "vacancy_id": vacancy_id,
                "checked_at": checked_at,
                "requested_url": url,
                "final_url": final_url,
                "html_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                "eligible": eligible,
                "reasons": reasons,
            }
            evidence_path = evidence_dir / f"{vacancy_id}-{checked_at[:10]}.json"
            evidence_path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
            payload["eligibility"] = {
                "eligible": eligible,
                "checked_at": checked_at,
                "evidence": str(evidence_path.resolve()),
                "evidence_vacancy_id": vacancy_id,
                "reasons": reasons,
            }
            if not args.dry_run:
                with con:
                    if eligible:
                        con.execute("UPDATE queue SET payload=?,last_error=NULL WHERE id=? AND state='pending'", (json.dumps(payload, ensure_ascii=False, sort_keys=True), row["queue_id"]))
                    else:
                        con.execute("UPDATE queue SET payload=?,state='failed',last_error=? WHERE id=? AND state='pending'", (json.dumps(payload, ensure_ascii=False, sort_keys=True), "eligibility:" + ",".join(reasons), row["queue_id"]))
            report["checked"] += 1
            report["eligible" if eligible else "rejected"] += 1
        except Exception as exc:
            report["errors"].append({"vacancy_id": vacancy_id, "error": f"{type(exc).__name__}:{exc}"})
    con.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["errors"] and not report["checked"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
