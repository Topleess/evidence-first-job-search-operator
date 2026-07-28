#!/usr/bin/env python3
"""Bounded HH scheduler runner: eligible queue -> adaptive executor -> watermark."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import subprocess
from urllib.parse import urlparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from local_funnel import LocalFunnel


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    temp.replace(path)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _canonical_candidate_url(value: object, external_id: str) -> str:
    parsed = urlparse(str(value or ""))
    host = (parsed.hostname or "").lower()
    if host not in {"hh.ru", "www.hh.ru", "hh.kz", "www.hh.kz"} or not external_id.isdigit():
        raise ValueError("invalid HH candidate URL")
    suffix = "kz" if host.endswith("hh.kz") else "ru"
    return f"https://hh.{suffix}/vacancy/{external_id}"


def select_candidates(db_path: str | Path, *, now: datetime, daily_cap: int, batch_limit: int, fresh_ttl_hours: int = 24) -> list[dict]:
    """Select only fresh, explicitly eligible HH application reviews with no receipt or prior intent."""
    if (
        isinstance(daily_cap, bool) or isinstance(batch_limit, bool) or isinstance(fresh_ttl_hours, bool)
        or daily_cap <= 0 or batch_limit <= 0 or fresh_ttl_hours <= 0
    ):
        raise ValueError("daily_cap, batch_limit and fresh_ttl_hours must be positive")
    db_path = Path(db_path)
    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        start = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        used = con.execute(
            """SELECT count(*) FROM (
                 SELECT external_vacancy_id AS vacancy_id FROM application_receipts
                  WHERE source='hh' AND submitted=1 AND read_back_verified=1 AND submitted_at>=?
                 UNION
                 SELECT COALESCE(json_extract(payload,'$.external_id'),json_extract(payload,'$.vacancy_id')) AS vacancy_id FROM action_intents
                  WHERE kind='application_submit' AND state IN ('reserved','executing','ambiguous','verified')
                    AND (
                      json_extract(payload,'$.source')='hh'
                      OR json_extract(payload,'$.url') LIKE 'https://hh.ru/vacancy/%'
                      OR json_extract(payload,'$.url') LIKE 'https://hh.kz/vacancy/%'
                    )
                    AND COALESCE(side_effect_maybe_at,execution_started_at,created_at)>=?
               )""", (start, start)
        ).fetchone()[0]
        remaining = max(0, daily_cap - int(used))
        if not remaining:
            return []
        fresh_cutoff = _iso(now.astimezone(timezone.utc) - timedelta(hours=fresh_ttl_hours))
        rows = con.execute(
            """SELECT id,payload FROM queue
               WHERE kind='application_review' AND state='pending'
                 AND available_at>=? AND available_at<=?
                 AND json_extract(payload,'$.source')='hh'
               ORDER BY available_at DESC,id""", (fresh_cutoff, _iso(now))
        ).fetchall()
        chosen: list[dict] = []
        seen_ids: set[str] = set()
        for row in rows:
            payload = json.loads(row["payload"])
            gate = payload.get("eligibility") or {}
            external_id = str(payload.get("external_id") or payload.get("source_job_id") or "").strip()
            try:
                checked = datetime.fromisoformat(str(gate.get("checked_at", "")).replace("Z", "+00:00"))
                if checked.tzinfo is None:
                    continue
                checked = checked.astimezone(timezone.utc)
            except ValueError:
                continue
            age = now.astimezone(timezone.utc) - checked
            if (
                gate.get("eligible") is not True
                or not str(gate.get("evidence") or "").strip()
                or str(gate.get("evidence_vacancy_id") or "") != external_id
                or age.total_seconds() < -300
                or age.total_seconds() > 86400
            ):
                continue
            if not external_id or external_id in seen_ids:
                continue
            receipt = con.execute(
                "SELECT 1 FROM application_receipts WHERE source='hh' AND external_vacancy_id=? LIMIT 1", (external_id,)
            ).fetchone()
            intent = con.execute(
                """SELECT 1 FROM action_intents
                   WHERE kind='application_submit'
                     AND state IN ('reserved','executing','ambiguous','verified','blocked')
                     AND COALESCE(json_extract(payload,'$.external_id'),json_extract(payload,'$.vacancy_id'))=?
                     AND (
                       json_extract(payload,'$.source')='hh'
                       OR json_extract(payload,'$.url') LIKE 'https://hh.ru/vacancy/%'
                       OR json_extract(payload,'$.url') LIKE 'https://hh.kz/vacancy/%'
                     ) LIMIT 1""",
                (external_id,),
            ).fetchone()
            if receipt or intent:
                continue
            chosen.append({
                **payload,
                "source_job_id": external_id,
                "source_url": _canonical_candidate_url(payload.get("job_url"), external_id),
                "title": payload.get("job_title"),
                "company_name": payload.get("company"),
                "queue_id": row["id"],
            })
            seen_ids.add(external_id)
            if len(chosen) >= min(batch_limit, remaining):
                break
        return chosen


def _valid_duplicate_evidence(result: dict, vacancy_id: str) -> bool:
    try:
        path = Path(str(result.get("evidence_path") or ""))
        data = json.loads(path.read_text(encoding="utf-8"))
        final_url = str(data.get("final_url") or "")
        parsed = urlparse(final_url)
        return (
            path.is_file() and str(data.get("id")) == vacancy_id
            and data.get("marker") == "already_applied_on_reopen"
            and parsed.scheme == "https" and not parsed.username and not parsed.password
            and not parsed.port and parsed.fragment == ""
            and parsed.hostname in {"hh.ru", "hh.kz"}
            and parsed.path == f"/vacancy/{vacancy_id}" and parsed.query == ""
        )
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def persist_terminal_results(funnel: LocalFunnel, db_path: str | Path, selected: list[dict], results: list[dict], *, now: datetime) -> None:
    by_id = {str(item.get("id") or ""): item for item in results}
    # Import historical duplicate evidence before opening the queue-update transaction.
    # Calling LocalFunnel.record_application while another sqlite connection holds a
    # read transaction can deadlock this single process with `database is locked`.
    for job in selected:
        vacancy_id = str(job.get("source_job_id") or "")
        result = by_id.get(vacancy_id)
        if not result or str(result.get("status") or "") != "duplicate":
            continue
        with sqlite3.connect(db_path, timeout=30) as check:
            duplicate_receipt = check.execute(
                "SELECT 1 FROM application_receipts WHERE source='hh' AND external_vacancy_id=? AND read_back_verified=1",
                (vacancy_id,),
            ).fetchone()
        if duplicate_receipt is None and _valid_duplicate_evidence(result, vacancy_id):
            funnel.record_application(
                source="hh", external_vacancy_id=vacancy_id,
                job_url=str(job.get("source_url") or job.get("job_url") or ""),
                company=str(job.get("company_name") or job.get("company") or ""),
                job_title=str(job.get("title") or job.get("job_title") or ""),
                status="already_applied", submitted_at=None, read_back_verified=True,
                evidence_path=str(result.get("evidence_path") or ""), channel="hh_browser_readback",
            )
    with sqlite3.connect(db_path, timeout=30) as con:
        for job in selected:
            vacancy_id = str(job.get("source_job_id") or "")
            result = by_id.get(vacancy_id)
            if not result:
                continue
            status = str(result.get("status") or "")
            if status == "dry_run_ready" and result.get("dry_run") is True:
                continue
            receipt = con.execute(
                "SELECT 1 FROM application_receipts WHERE id=? AND source='hh' AND external_vacancy_id=? AND submitted=1 AND read_back_verified=1",
                (result.get("receipt_id") or -1, vacancy_id),
            ).fetchone()
            duplicate_receipt = con.execute(
                "SELECT 1 FROM application_receipts WHERE source='hh' AND external_vacancy_id=? AND read_back_verified=1",
                (vacancy_id,),
            ).fetchone()
            proven_verified = status == "verified" and receipt is not None
            proven_duplicate = status == "duplicate" and duplicate_receipt is not None
            queue_state = "done" if proven_verified or proven_duplicate else "failed"
            if queue_state != "done":
                result["status"] = f"blocked_unproven_{status or 'missing_status'}"
                result["submitted"] = False
                result["read_back_verified"] = False
            con.execute("UPDATE queue SET state=?,last_error=? WHERE id=? AND state='pending'",
                        (queue_state, f"hh_{status}"[:300], int(job["queue_id"])))
        con.commit()


def summarize_results(selected: list[dict], results: list[dict]) -> dict:
    by_id = {str(item.get("id") or item.get("external_vacancy_id") or ""): item for item in results}
    blockers: list[dict] = []
    verified = 0
    for job in selected:
        vacancy_id = str(job.get("source_job_id") or job.get("external_id") or "")
        result = by_id.get(vacancy_id)
        if result is None:
            blockers.append({"id": vacancy_id, "status": "missing_result"})
            continue
        status = str(result.get("status") or "")
        if status == "verified" and result.get("submitted") is True and result.get("read_back_verified") is True and result.get("receipt_id"):
            verified += 1
        elif status == "duplicate" and result.get("submitted") is True and result.get("read_back_verified") is True and result.get("evidence_path"):
            # Reconciled historical submission: successful dedupe/read-back, not a new send.
            pass
        elif status == "dry_run_ready" and result.get("dry_run") is True:
            pass
        else:
            blockers.append(result)
    return {"verified": verified, "blockers": blockers}


def recover_stale_executing(db_path: str | Path, *, now: datetime, stale_seconds: int = 1800) -> int:
    cutoff = (now.astimezone(timezone.utc) - timedelta(seconds=stale_seconds)).isoformat()
    timestamp = now.astimezone(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as con:
        con.execute("BEGIN IMMEDIATE")
        updated = con.execute(
            """UPDATE action_intents
               SET state='ambiguous', updated_at=?, side_effect_maybe_at=COALESCE(side_effect_maybe_at,execution_started_at),
                   last_error_code='stale_executing_requires_readback', executing_by=NULL, execution_token=NULL,
                   next_reconcile_at=?
               WHERE kind='application_submit' AND state='executing'
                 AND json_extract(payload,'$.source')='hh' AND execution_started_at<=?""",
            (timestamp, timestamp, cutoff),
        )
        con.commit()
        return updated.rowcount


def preserve_run_ambiguity(db_path: str | Path, *, run_id: str, now: datetime) -> int:
    timestamp = now.astimezone(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as con:
        con.execute("BEGIN IMMEDIATE")
        updated = con.execute(
            """UPDATE action_intents
               SET state='ambiguous',updated_at=?,side_effect_maybe_at=COALESCE(side_effect_maybe_at,execution_started_at),
                   last_error_code='executor_exception_requires_readback',executing_by=NULL,execution_token=NULL,next_reconcile_at=?
               WHERE run_id=? AND kind='application_submit' AND state='executing'""",
            (timestamp, timestamp, run_id),
        )
        con.commit()
        return updated.rowcount


def recover_stale_batches(db_path: str | Path, *, now: datetime, stale_seconds: int = 1800) -> tuple[int, int]:
    cutoff = (now.astimezone(timezone.utc) - timedelta(seconds=stale_seconds)).isoformat()
    timestamp = now.astimezone(timezone.utc).isoformat()
    with sqlite3.connect(db_path) as con:
        con.execute("BEGIN IMMEDIATE")
        stale_ids = [row[0] for row in con.execute(
            "SELECT id FROM batch_runs WHERE channel='hh' AND state='running' AND COALESCE(heartbeat_at,started_at)<=?",
            (cutoff,),
        )]
        if not stale_ids:
            con.commit()
            return 0, 0
        marks = ','.join('?' for _ in stale_ids)
        reserved = con.execute(
            f"""UPDATE action_intents SET state='blocked',updated_at=?,last_error_code='stale_reserved_batch_closed'
                WHERE state='reserved' AND run_id IN ({marks})""",
            (timestamp, *stale_ids),
        ).rowcount
        con.execute(
            f"""UPDATE batch_runs SET state='failed',finished_at=?,heartbeat_at=?,stop_reason='stale_batch_recovered'
                WHERE id IN ({marks}) AND state='running'""",
            (timestamp, timestamp, *stale_ids),
        )
        con.commit()
        return len(stale_ids), reserved


def finish_run(funnel: LocalFunnel, run_id: str, *, exit_code: int, now: datetime) -> None:
    state = "completed" if exit_code == 0 else "failed"
    reason = "executor_completed" if exit_code == 0 else f"executor_exit_{exit_code}"
    funnel.finish_batch_run(run_id=run_id, state=state, reason=reason, now=now)


@contextmanager
def exclusive_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("hh_runner_already_active") from exc
        yield


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="state/job_funnel.sqlite3")
    parser.add_argument("--profile", default="data/browser_profiles/hh_ru")
    parser.add_argument("--daily-cap", type=int, default=20)
    parser.add_argument("--batch-limit", type=int, default=5)
    parser.add_argument("--fresh-ttl-hours", type=int, default=24)
    parser.add_argument("--truth-map", default="")
    parser.add_argument("--state-dir", default="state/hh-cron")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    lock_path = Path(args.db).resolve().with_suffix('.hh-runner.lock')
    with exclusive_lock(lock_path):
        stale_batches, stale_reserved = recover_stale_batches(args.db, now=now)
        recovered = recover_stale_executing(args.db, now=now)
        jobs = select_candidates(
            args.db, now=now, daily_cap=args.daily_cap,
            batch_limit=args.batch_limit, fresh_ttl_hours=args.fresh_ttl_hours,
        )
        initial_blockers = ([{"status": "stale_executing_requires_readback", "count": recovered}] if recovered else [])
        if stale_batches:
            initial_blockers.append({"status": "stale_batch_recovered", "count": stale_batches, "reserved_blocked": stale_reserved})
        watermark = {
            "started_at": _iso(now), "selected": len(jobs), "candidate_count": len(jobs),
            "verified": 0, "blockers": initial_blockers,
            "recovered_stale_executing": recovered, "status": "empty",
            "fresh_ttl_hours": args.fresh_ttl_hours,
        }
        if not jobs:
            if initial_blockers:
                watermark["status"] = "blocked"
            atomic_json(state_dir / "watermark.json", watermark)
            print(json.dumps(watermark, ensure_ascii=False))
            return 1 if initial_blockers else 0
        funnel = LocalFunnel(path=Path(args.db))
        run_id = None
        output_path = state_dir / "output-unstarted.json"
        try:
            run_id = funnel.begin_batch_run(channel="hh", max_actions=args.batch_limit, started_at=now)
            input_path = state_dir / f"input-{run_id}.json"
            output_path = state_dir / f"output-{run_id}.json"
            input_path.write_text(json.dumps(jobs, ensure_ascii=False, indent=2))
            command = ["node", "scripts/hh_adaptive_executor.js", "--input", str(input_path), "--run-id", run_id,
                       "--db", args.db, "--profile", args.profile, "--limit", str(args.batch_limit), "--daily-cap", str(args.daily_cap),
                       "--output", str(output_path), "--evidence-dir", str(state_dir / "evidence" / run_id)]
            if args.truth_map:
                truth_map = Path(args.truth_map)
                if not truth_map.is_file():
                    raise RuntimeError("truth_map_missing")
                command.extend(["--truth-map", str(truth_map)])
            if args.dry_run:
                command.append("--dry-run")
            executor_env = dict(os.environ)
            # HH closes TLS connections routed through the global SOCKS proxy on this host.
            # The authenticated Playwright profile must use the verified direct route.
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                executor_env.pop(key, None)
            executor_env["PLAYWRIGHT_BROWSERS_PATH"] = str(Path.cwd() / ".playwright")
            completed = subprocess.run(command, text=True, capture_output=True, timeout=900, env=executor_env)
            if not output_path.exists():
                raise RuntimeError("executor_output_missing")
            results = json.loads(output_path.read_text())
            if not isinstance(results, list):
                raise RuntimeError("executor_output_invalid")
            finished_at = datetime.now(timezone.utc)
            if completed.returncode != 0:
                preserve_run_ambiguity(args.db, run_id=run_id, now=finished_at)
            persist_terminal_results(funnel, args.db, jobs, results, now=finished_at)
            summary = summarize_results(jobs, results)
            combined_blockers = initial_blockers + summary["blockers"]
            # A deterministic item-level blocker (unknown required answer, missing
            # apply entry, assessment, etc.) is a safe terminal result for that item,
            # not a channel outage. The contract explicitly requires continuing other
            # safe items/channels. Only executor/system failure or recovery ambiguity
            # makes the HH batch fail.
            effective_ok = completed.returncode == 0 and not initial_blockers
            finish_run(funnel, run_id, exit_code=0 if effective_ok else (completed.returncode or 1), now=finished_at)
            watermark.update({
                "run_id": run_id, "finished_at": _iso(finished_at),
                "exit_code": completed.returncode,
                "status": ("ok_with_blocked_items" if summary["blockers"] else "ok") if effective_ok else "failed",
                "verified": summary["verified"], "blockers": combined_blockers, "output": str(output_path),
            })
        except Exception as exc:
            finished_at = datetime.now(timezone.utc)
            try:
                if run_id is not None:
                    preserve_run_ambiguity(args.db, run_id=run_id, now=finished_at)
                    finish_run(funnel, run_id, exit_code=1, now=finished_at)
            except ValueError:
                pass
            watermark.update({
                "run_id": run_id, "finished_at": _iso(finished_at), "exit_code": 1, "status": "failed",
                "blockers": initial_blockers + [{"status": "runner_exception", "error": str(exc)[:300]}], "output": str(output_path),
            })
            effective_ok = False
        atomic_json(state_dir / "watermark.json", watermark)
        if watermark["verified"] or watermark["blockers"]:
            print(json.dumps(watermark, ensure_ascii=False))
        return 0 if effective_ok else int(watermark.get("exit_code") or 1)


if __name__ == "__main__":
    raise SystemExit(main())
