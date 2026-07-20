# Autonomous Job Search Operator Goal

## Objective
Build and launch one autonomous production control plane in `/opt/data/job-search` for vacancy discovery, eligibility, evidence-backed tailoring, HH/LinkedIn/ATS applications, vacancy-specific Gmail HR outreach, reply reconciliation, and at most one follow-up.

## Authoritative scope
- Production workspace: `/opt/data/job-search`
- Existing implementation/runtime sources to reconcile: `/opt/data/job-search-agent-hh`, `/opt/data/job-search-agent-linkedin`, `/opt/data/scripts`, `/opt/data/cron/jobs.json`
- Import reviewed source/tests only. Do not import secrets, cookies, browser profiles, live DBs, screenshots, receipts, caches, or recipient lists blindly.

## Operating contract
1. Fresh source read and live vacancy eligibility before every initial action.
2. Targets: remote/relocation AI PM, Product/Ops/Project Lead, Founder Associate; acceptable ops/admin roles.
3. Exclude developer, design, marketing, and support roles. Salary floor approximately 130k RUB net when salary is known/applicable.
4. Truthful evidence-backed CV, cover letters, ATS answers, and outreach only.
5. Durable fenced intent before every external write.
6. Exactly one side effect, then independent platform/provider read-back.
7. Ambiguous outcome is fenced and reconciled before any retry.
8. Unknown required answer, assessment, CAPTCHA, OTP, or auth loss blocks only that item.
9. Quota ceilings per working cycle: HH 20, LinkedIn 5, HR email 5. Never pad batches.
10. At most one follow-up after inbox/thread reconciliation.

## Acceptance
For every available channel: one fresh eligible real canary, authoritative receipt in the unified SQLite, and a second bounded scheduled run proving no duplicate and correct run attribution. Report each channel as `ready`, `conditionally ready`, or `blocked`.

## Current execution checkpoint
- Goal activated: 2026-07-19.
- Parallel evidence-first audits dispatched for HH, LinkedIn, and ATS/Gmail.
- Existing cron jobs and SQLite ledgers inspected; integration/convergence is pending.
