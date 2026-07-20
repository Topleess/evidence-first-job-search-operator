# Vacancy-specific application policy

Status: normative private policy for generated resumes, cover letters and verified recruiter-email drafts.
Research date: 2026-07-16.

## Principle

Tailoring means evidence selection, truthful terminology alignment, and clear rendering. It is not ATS manipulation. Do not generate or display a universal "ATS score".

## Trusted inputs

Every candidate claim must reference a stable `fact_id` with:

- claim;
- source type and locator;
- confidence: `verified`, `user_attested`, or `unverified`;
- allowed transformations.

Only `verified` and `user_attested` facts may enter a submitted document. A metric also requires its source fact and measurement basis. The system must never infer percentages, revenue, user counts, team ownership, seniority, certificates, language level, work authorization, or tool experience.

Every vacancy requirement must preserve an exact quote and source location. Classify it as `must_have`, `preferred`, `responsibility`, `domain_term`, `tool`, `credential`, `seniority_scope`, or `location_work_authorization`.

## Evidence matching

Use these weights for an internal evidence coverage report:

- required: 3;
- preferred: 1;
- contextual: 0.25.

Report separately:

- `required_evidence_coverage`;
- `preferred_evidence_coverage`;
- `unsupported_keyword_count`;
- `missing_required_requirements`.

Keywords may be used only when backed by candidate evidence. Exact vacancy terminology and a common equivalent may appear naturally together. Never copy whole vacancy sentences, stuff repeated keywords, use hidden text, micro-fonts, or white-on-white content.

## Allowed transformations

Allowed:

- rewrite Summary for the target role;
- reorder existing bullets within an experience;
- select relevant bullets from the master fact bank;
- reorder Skills, Projects, and Certifications;
- shorten old irrelevant experience;
- translate without changing meaning;
- expand a standard abbreviation;
- create a truthful target headline.

Forbidden:

- change employer, official title, dates, employment relationship, or seniority;
- turn team outcomes into personal ownership;
- add tools, credentials, sectors, languages, metrics, or responsibilities without provenance;
- claim causality when evidence proves only participation;
- hide gaps through false date aggregation.

If a display title is enriched, the normalized official title must remain present unless an approved alias exists.

## Achievement bullets

Preferred structure: action + scope/context + method/tool + demonstrated result.

A number is allowed only with a linked fact and measurement basis. Qualitative outcomes are valid when truthful. Ban unsupported intensifiers such as "significantly", "dramatically", and "substantially" unless followed by concrete evidence.

## Cover letter

Default constraints unless the form requests otherwise:

- 100–250 words, target 120–220;
- 3–5 paragraphs;
- 2 or more provenance-backed evidence points;
- at least one job-specific reference;
- at least one verifiable company/task reference;
- exactly one CTA;
- zero unsupported claims.

Flag clichés and replace them with evidence, not synonyms. Examples: "I am writing to express my interest", "thrilled to apply", "great fit", "dynamic team", "esteemed organization", "passion for innovation", "unique blend", "proven track record", "leverage my skills", "aligns perfectly", "I am confident that".

Do not use probabilistic AI detectors as a gate. Gate observable defects: missing provenance, copy-pasted resume text, no concrete vacancy task, generic company praise, repeated openings, and excessive abstractions.

Company-swap test: if replacing company and title leaves at least 80% of the letter appropriate, block it as too generic.

## Verified recruiter email

Default constraints:

- 40–120 words;
- exact vacancy title;
- 1–2 evidence points;
- exactly one CTA;
- zero unsupported claims;
- recipient identity and relationship to vacancy must have provenance;
- do not send duplicate messages to several employees for the same vacancy.

## Document validation

Use standard section headings and keep contact data in the primary text flow. Critical content must not exist only in images, headers/footers, floating shapes, text boxes, or complex tables. One column is preferred but not dogmatic: extracted reading order is the actual gate.

PDF is allowed only when the form allows it, it has a valid text layer, extraction preserves reading order, encoding is intact, and the file opens successfully. DOCX is allowed only when the form allows it, OOXML parses successfully, tracked changes/comments are absent, and critical information is not isolated in floating objects or headers.

## Blocking acceptance gates

Before any durable submit/send intent can enter execution:

1. Every factual sentence maps to one or more permitted fact IDs.
2. `unsupported_keyword_count == 0`.
3. Official employers, titles, dates, seniority, and metrics match the fact bank.
4. No hidden text, keyword stuffing, suspicious micro-fonts, or copied vacancy sentences.
5. Extracted document text contains name/contact and required sections in usable order.
6. File extension, MIME type, and size match the exact application form.
7. Cover letter/email meets length, specificity, evidence, and CTA rules.
8. Unknown required application questions block only that vacancy and are stored as a normalized question manifest.
9. Generated artifacts and their provenance manifest are immutable inputs to the action intent digest.

## Evidence sources

- LinkedIn Future of Recruiting: https://business.linkedin.com/hire/resources/future-of-recruiting
- LinkedIn AI Resource Hub: https://business.linkedin.com/hire/resources/ai-resource-hub
- Greenhouse Job Board API: https://developers.greenhouse.io/job-board.html
- Lever developer documentation: https://hire.lever.co/developer/documentation
- Indeed ATS resume guidance: https://www.indeed.com/career-advice/resumes-cover-letters/ats-resume
- Indeed resume keywords: https://www.indeed.com/career-advice/resumes-cover-letters/resume-keywords
- NACE Career Readiness Competencies: https://www.naceweb.org/career-readiness/competencies/career-readiness-defined/
- Harvard FAS resumes and cover letters: https://careerservices.fas.harvard.edu/channels/create-a-resume-cv-or-cover-letter/

## Evidence limitations

No primary source supports a universal ATS passing score, mandatory 80–90% keyword match, one universally superior file format, or automatic rejection based on a single hidden ATS number. Exact form constraints and extracted-document quality govern acceptance.
