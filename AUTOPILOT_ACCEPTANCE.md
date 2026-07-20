# Hermes Job Search Autopilot — acceptance report

Date: 2026-07-12 UTC

## Verdict
Accepted. The durable operator loop, external application path, task transition, and blocker notifier were exercised with real outputs.

## Durable state
- SQLite: `/opt/data/job-search/state/autopilot.sqlite3`
- WAL enabled.
- Queue CLI: `scripts/autopilot_queue.py`
- Atomic claim: `BEGIN IMMEDIATE` + status guard.
- States: `ready`, `running`, `blocked`, `done`, `failed`.
- Event history records claim, completion, block, reminder, and resolution.

## Scheduler
- Operator job: `bad4de1548a7`
  - schedule: every 30 minutes
  - pinned model: `openai-codex / gpt-5.6-sol`
  - verified status: `ok`
- Blocker notifier: `2cc7b35eadb2`
  - schedule: every minute
  - script-only, silent when no blocker
  - verified status: `ok`

## Root cause repaired
The old operator was unpinned and Hermes correctly refused to spend after global model drift from `gpt-5.5` to `gpt-5.6-sol`. The job is now explicitly pinned and executed successfully.

## Real external actions
1. LinkedIn Easy Apply job `4439083216` (Nexify Infosystems)
   - claimed by queue
   - application submitted
   - post-action read-back: `Bewerbungsstatus / Bewerbung gesendet / Gerade`
   - evidence: `applications/linkedin_nexify/result.json`
2. HH safe batch
   - РТЛабс `132195551`: `applied=true`, `cover_sent=true`, read-back `Резюме доставлено`
   - VK / Учи.ру `134187468`: `applied=true`, `cover_sent=true`, read-back `Резюме доставлено`
   - evidence: `data/acceptance/first_safe_batch_final_2026-07-12.json`

## Autonomous transition evidence
Queue events show:
- `linkedin-nexify-submit`: ready → claimed → done
- `first-safe-batch`: ready → claimed → done
- `followups`: remains ready as the next scheduled task

## Blocker test
Temporary task `verifier-blocker-test` exercised:
- blocked with a concrete reason and instruction
- reminder emitted
- task resolved
- reminder state cleared
- task completed

Reminder policy: once per minute for the first ten notifications, then once per ten minutes; silent after resolution.
