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

## Local verification

Python 3.11+ and Node.js 20+ are recommended.

```bash
npm ci
uv run --with pytest pytest -q scripts/test_*.py
node --test scripts/test_hh_form_browser.js scripts/test_hh_form_state.js
```

Run the unified control plane without external actions:

```bash
python3 scripts/job_search_operator.py --hh-cap 1 --linkedin-cap 1 --email-cap 1
```

External actions require the explicit `--execute` flag and correctly configured local credentials/browser profiles:

```bash
python3 scripts/job_search_operator.py --execute --hh-cap 20 --linkedin-cap 5 --email-cap 5
```

Review each adapter and configure conservative limits before enabling execution.

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
