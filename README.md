# Evidence-First Autonomous Job Search Operator

A local, fail-closed control plane for collecting vacancies, applying strict eligibility rules, and executing job applications or recruiter outreach with durable exactly-once safeguards.

## Why this exists

Most job-search automation stops after clicking **Submit**. This project treats the external platform as the source of truth:

```text
collect → normalize/dedupe → hard eligibility → tailor from approved facts
→ durable intent → exactly one side effect → independent read-back receipt
→ reply reconciliation → at most one follow-up
```

A process exit code or browser click is not considered proof that an application was sent.

## Supported channels

| Channel | Collection | Fenced execution | Independent read-back |
|---|---:|---:|---:|
| HH.ru | Yes | Yes | Application-history verification |
| LinkedIn | Public discovery | Easy Apply | Platform receipt/state |
| Greenhouse/Ashby ATS | Yes | Deterministic forms only | Provider/platform confirmation |
| Gmail recruiter outreach | Candidate-specific | Gmail API | Exact `Message-ID` in Sent |

## Safety model

- One authoritative SQLite database.
- A durable intent is committed before any external side effect.
- Execution tokens bind the worker to one intent and one vacancy.
- Unknown required answers, CAPTCHA, OTP, assessments, auth loss, or unclear eligibility block only that item.
- Ambiguous sends are reconciled, never automatically retried.
- Quotas are ceilings, not targets.
- Tailoring may use only approved, evidence-backed candidate facts.
- Browser profiles, OAuth credentials, runtime state, messages, resumes, and other PII are excluded from Git.

## Repository map

```text
scripts/job_search_operator.py        unified bounded runner
scripts/local_funnel.py               SQLite intents, receipts and reconciliation
scripts/hh_cron_runner.py             HH bounded production runner
scripts/linkedin_cron_runner.py       LinkedIn bounded production runner
scripts/ats_cron_runner.py            ATS bounded production runner
scripts/email_cron_runner.py          Gmail outreach and single follow-up
scripts/email_response_reconciler.py  replies and bounce reconciliation
scripts/export_public_case_evidence.py redacted evidence exporter
schemas/                              local data contracts
public-case/evidence.json             sanitized example evidence
```

## Runtime boundary

The public repository intentionally does **not** include:

```text
state/ data/ profile/ applications/ outreach/ reports/ sheets/
.env files, OAuth tokens, browser sessions, resumes, message bodies, databases
```

Create those locally. The production workspace expects an authoritative database at:

```text
state/job_funnel.sqlite3
```

## Quick start: what actually runs after cloning

The repository is currently a **reference implementation plus tested channel workers**, not a one-command hosted application. It intentionally excludes credentials, candidate data, browser sessions, production SQLite, and local collector wrappers. Therefore `--execute` cannot safely work from a clean clone until you supply those runtime assets.

### 1. Clone and verify the code

```bash
git clone https://github.com/Topleess/evidence-first-job-search-operator.git
cd evidence-first-job-search-operator

# Install uv if it is not available: https://docs.astral.sh/uv/
npm ci
npx playwright install chromium

uv run --with-requirements requirements-test.in pytest -q scripts/test_*.py
node --test scripts/test_hh_form_browser.js scripts/test_hh_form_state.js
```

Expected result:

```text
127 Python tests passed
20 Node.js tests passed
```

This verifies the state machine, fences, selectors, form classifiers, read-back contracts, and duplicate protection. It does **not** send an application.

### 2. Understand the entrypoint

```bash
python3 scripts/job_search_operator.py --help
```

The intended safe command is:

```bash
python3 scripts/job_search_operator.py --hh-cap 1 --linkedin-cap 1 --email-cap 1
```

However, on a clean clone it currently exits nonzero because the private runtime assets listed below are absent. That is deliberate fail-closed behavior, not a successful demo mode.

### 3. Runtime assets required for real operation

| Asset | Purpose | Public repo status |
|---|---|---|
| `state/job_funnel.sqlite3` | Authoritative jobs, intents and receipts | Excluded; contains operational data |
| Candidate fact profile | Truthful answers and tailoring evidence | Excluded; contains PII |
| HH browser profile | Authenticated apply/read-back | Excluded |
| LinkedIn browser profile | Authenticated Easy Apply/read-back | Excluded |
| Google OAuth token | Gmail Sent/read reconciliation | Excluded |
| Collector wrappers | Source-specific scheduled collection | Production host currently uses local `/opt/data/scripts/...` wrappers |

### 4. Production command after runtime configuration

Only after those assets are configured and a dry run succeeds:

```bash
python3 scripts/job_search_operator.py \
  --execute \
  --hh-cap 20 \
  --linkedin-cap 5 \
  --email-cap 5
```

`--execute` enables real external side effects. Start with caps of `1`, inspect the durable intent and independent receipt, and never copy another person's browser/OAuth/profile state.

## What is in this repository

This is the safety-critical orchestration layer:

- collection adapters and normalization;
- strict eligibility selectors;
- evidence-backed tailoring contracts;
- SQLite intent/receipt/reconciliation state machine;
- HH, LinkedIn, ATS, and Gmail bounded workers;
- browser form classifiers that fail closed on unknown questions;
- duplicate and ambiguous-outcome protection;
- redacted evidence exporter;
- regression and browser contract tests.

It is **not yet** a packaged installer, Docker Compose application, web UI, or reusable account-onboarding wizard.

## Local verification details

Python 3.11+ and Node.js 20+ are recommended.

```bash
npm ci
npx playwright install chromium
uv run --with-requirements requirements-test.in pytest -q scripts/test_*.py
node --test scripts/test_hh_form_browser.js scripts/test_hh_form_state.js
```

## Public evidence

Generate a sanitized package from the local authoritative database:

```bash
python3 scripts/export_public_case_evidence.py
```

The exporter omits candidate identity, email addresses, message bodies, OAuth data, browser profiles, intent payloads, and execution tokens.

## Status

The architecture and HH end-to-end canary are proven in the included sanitized evidence. Other channels remain fail-closed until a naturally eligible candidate/contact is available and all required answers are supported by approved facts.

## License

No license has been selected yet. Treat the repository as source-available unless the owner adds one.
