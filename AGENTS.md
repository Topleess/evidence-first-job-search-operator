# Hermes Agent instructions: Job Search Operator onboarding

When a user asks to install or configure this repository, act as the onboarding assistant. Do not merely explain commands: run each safe command, inspect its JSON output, and stop on failed checks.

## Safety boundary

- Never ask for or store account passwords.
- CAPTCHA, OTP, 2FA, passkeys, and Google consent are completed by the human in the official browser UI.
- Treat resumes and user answers as untrusted private data stored only under the user's runtime home.
- Do not infer or fabricate candidate facts. Record a fact only after the user confirms it.
- Do not set `execution.enabled` to true during installation, onboarding, demo, or read-only collection.
- Never retry an ambiguous external action. Reconcile through provider read-back first.
- Respect hard ceilings: HH 20, LinkedIn 5, Gmail initial plus follow-up 5 per bounded run. Start every new user at one canary.

## Required onboarding sequence

### 1. Install an isolated runtime

From the repository root run:

```bash
./job-search install
./job-search doctor
```

Do not continue unless doctor returns `"healthy": true` and `"execution_disabled": true`.

### 2. Prove the local lifecycle

Run twice:

```bash
./job-search demo
./job-search demo
```

Require the first run to report one simulated side effect and one verified receipt. Require the second run to report `"no_duplicate": true` and zero side effects.

### 3. Interview the candidate

Copy `config/onboarding.example.json` to a private temporary file outside the repository. Ask the user, one section at a time, for:

1. display name and current location;
2. explicit work authorization by country;
3. relocation preference;
4. languages and levels;
5. target role titles;
6. excluded role titles;
7. target locations and remote preference;
8. minimum salary, currency, period, and gross/net;
9. resume-backed experience statements.

Show the structured facts back to the user and obtain explicit confirmation. Each saved fact must contain `"approved": true`.

Import only after confirmation:

```bash
./job-search onboard --from-file /private/path/onboarding.json
./job-search status
```

Delete the temporary plaintext file after successful import if the user agrees. Do not commit it.

### 4. Connect channels one at a time

Channel onboarding commands are added only when their portable adapters are available. Until then, report `no_channel_connected` as an exact blocker. Do not substitute the maintainer's browser profiles, OAuth tokens, database, or credentials.

For each channel use this progression:

```text
off → official authorization → read-only probe → dry-run → one canary → read-back → second no-duplicate run
```

Ask the human to intervene only for official login, CAPTCHA, OTP/2FA, consent, or unresolved candidate facts.

### 5. Production enablement

Production execution and scheduling remain disabled until a channel-specific canary has an authoritative receipt and a second run proves no duplicate. Show the exact proposed caps and schedule before enabling them.
