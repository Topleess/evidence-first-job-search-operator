# Job Search Operator — verified integration status

Updated: 2026-07-19 23:43 UTC

## Authoritative runtime
- Workspace: `/opt/data/job-search`
- SQLite: `/opt/data/job-search/state/job_funnel.sqlite3`
- Scheduler registry: `/opt/data/cron/jobs.json`
- `/opt/data/job-search-agent-hh` and `/opt/data/job-search-agent-linkedin` are nonauthoritative legacy/evidence workspaces.
- Unified entrypoint: `/opt/data/job-search/scripts/job_search_operator.py`.
- Redacted public-case exporter: `/opt/data/job-search/scripts/export_public_case_evidence.py`; current validated package: `/opt/data/job-search/public-case/evidence.json` (SQLite integrity `ok`, no ambiguous intents, automated email/token-field scan passed).
- Verified unified dry cycle with reply reconciliation evidence: `state/operator-runs/2026-07-19T205519.396823+0000.json`; HH, LinkedIn, ATS and Gmail were invoked, then Gmail thread reply reconciliation completed exit 0 without side effects.

## Evidence inventory
- HH: 9 historical submitted/read-back receipts plus one read-back-only historical dedupe receipt added as `already_applied`, not counted as a new send.
- LinkedIn: 3 submitted/read-back receipts; one (`4440756817`) has full fenced intent → execution → read-back evidence.
- Gmail: one provider-backed SENT/read-back receipt exists outside the authoritative SQLite path. The authoritative runtime now includes OAuth Gmail API transport and exact `Message-ID` Sent read-back; live OAuth/read-only lookup succeeded on 2026-07-19.
- ATS: collector and fail-closed primitives exist; no production submission scheduler or accepted canary yet.
- Two legacy ambiguous application intents were reconciled on 2026-07-19 and permanently moved to `blocked` without receipts or retries. Evidence: `state/reconciliation/intent-1-blocked.json` and `state/reconciliation/intent-2-blocked.json`; authoritative ambiguous count is now 0.

## Readiness
| Channel | Status | Primary blocker |
|---|---|---|
| HH | ready | Fresh strict canary verified: vacancy `135244629`, intent `6`, receipt `22`, submitted/read-back at `2026-07-19T21:36:23.899000+00:00`, run `65749f00db354326ab48933b78b1e5a2`. Second production-mode bounded run at `21:40:43Z` selected 0; authoritative counts remain one intent/one receipt. |
| LinkedIn | conditionally ready | Bounded production runner, authoritative run attribution, strict candidate selector, intent/receipt bridge and stale crash fencing are implemented. Fresh eligible Easy Apply candidate is currently absent; schedule remains disabled until first canary. |
| ATS | conditionally ready | Single-action bounded runner and strict Ashby selector now gate on hard eligibility, geo, salary, freshness, form snapshot, deterministic answers and artifacts. Two-phase worker uses authoritative intents/receipts and safe pre-submit/ambiguous transitions. No fresh fully evidenced ATS candidate exists yet. |
| Gmail | conditionally ready | OAuth send, exact Sent/read reply reconciliation, authoritative receipts, vacancy-specific selector, and one-follow-up execution are integrated. Initial outreach plus follow-ups share one batch run and one total ceiling of 5. Current drafts still lack sufficient vacancy-specific provenance. |

## Repairs completed in current integration
- Job-board collection uses split routing: Remotive through the configured proxy, GeekJob/Getmatch and the other boards direct. Both source runs must succeed, then `merge_job_board_split_artifacts.py` creates one latest authoritative artifact. Verified merge: 66 rows; SQLite sync accepted 66, created 43, updated 23.
- HH collector bypasses the broken global SOCKS route and fails nonzero on repeated source errors.
- Fresh HH collection produced 35 rows; local sync queued 29.
- Added conservative live HH eligibility evidence stage; 30 checked, 25 eligible, 5 rejected.
- Canonicalized HH vacancy URLs and supported currently observed safe HH apply query keys.
- HH Playwright subprocess bypasses the broken SOCKS route; live authenticated probe succeeded.
- Historical HH duplicate read-back is persisted as non-submission evidence and cannot consume quota.
- Legacy HH ambiguous payloads (`vacancy_id` without `source/external_id`) are now included in pre-submit dedupe and quota fences.
- LinkedIn authoritative intent preparation now checks unified `application_receipts` before reserving an intent, including imported read-back-only/already-applied receipts. Live DB proof: `4440756817=True`, unseen `4453058520=False`.

## Latest tests
- Focused Python HH integration: 14 passed.
- HH JavaScript state contract: 10 passed.
- Wider local funnel/reliability suite earlier in this run: 63 passed, 19 subtests passed.

## Acceptance still outstanding
1. LinkedIn: wait for a fresh strict Easy Apply candidate, then require intent → one submit → independent receipt → second scheduled no-duplicate run.
2. ATS: n8n candidate remains item-level blocked on unknown required candidate facts; do not submit or retry until facts are supplied. Continue selecting other deterministic ATS forms.
3. Gmail: wait for a vacancy-specific verified recipient and complete fresh send/read-back plus second scheduled no-duplicate run.
4. Unified production scheduler is enabled as job `bad4de1548a7` (`0 11,15,19 * * *`, wrapper `/opt/data/scripts/job_search_unified_operator.sh`). Legacy standalone HH executor `0ffa791df94f` is paused to preserve one control-plane owner.
5. Observe the first two unified scheduled executions and record their artifacts/run IDs; channel selectors remain fail-closed when no eligible candidate exists.
