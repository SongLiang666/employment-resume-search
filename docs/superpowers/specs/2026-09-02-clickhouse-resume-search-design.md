# ClickHouse Resume Search Skill Design

## Goal

Create a distributable Codex skill that converts Chinese natural-language resume criteria into safe ClickHouse queries and returns one summary row per matching resume. Install the working copy at `~/.codex/skills/clickhouse-resume-search` and publish the reusable skill through GitHub without publishing credentials.

## Repository Layout

```text
employment-resume-search/
├── README.md
├── docs/superpowers/specs/
└── skills/clickhouse-resume-search/
    ├── SKILL.md
    ├── agents/openai.yaml
    ├── scripts/search_resumes.py
    ├── references/schema.md
    └── config/connection.example.json
```

The locally installed skill additionally contains `config/connection.json`. Git must ignore this file. Store the real ClickHouse URL, username, and password only in that local file and set its permissions to `0600`.

## Data Sources

Use these tables from `RCW_RC_Voodoo_Jobseeker`:

- `JobSeekerResume` as the primary resume table.
- `JobSeekerBaseInfo` for job-seeker account and base attributes when needed.
- `JobSeekerResumeExtension` for self-evaluation, specialty names, career names, salary, and other extended attributes.
- `JobSeekerResumeExperience` for employment history. Treat `ExperienceType = 2` as employment; `ExperienceText1` is the organization name and `ExperienceText2` is the position or career label.
- `JobSeekerResumeProjectExperience` for project and training keywords.

Join by `ResumeID` where available and by `JobSeekerID` only when necessary. Do not select or expose `JobSeekerPassword`.

## Query Behavior

Apply these predicates to every search:

```sql
r.DelFlag = 0
AND r.ResumeState = 2
AND r.ResumeGuid IS NOT NULL
```

Use `LIMIT 100` unless the user explicitly requests another positive result count. Honor the requested count. Sort by `r.LastRefreshDate DESC, r.LastEditTime DESC` unless the user explicitly requests another order.

Translate clearly named numeric, date, and text conditions directly. Use ClickHouse typed query parameters for all user-provided values. Never interpolate user text into SQL literals.

For an unqualified free-text keyword, search across relevant fields in the resume, extension, employment, and project tables. Multiple keywords must all match; each keyword may match any eligible field. Use `IN` subqueries or joins that preserve one output row per resume.

Do not guess undocumented enum mappings such as education codes. If a requested natural-language value cannot be mapped from verified schema or reference data, explain the ambiguity and ask for the numeric code or an approved mapping before querying.

## Result Shape

Return one compact summary object per resume. Always include a non-null `ResumeGuid`. Include, when available:

- `ResumeGuid`, `ResumeID`, and `JobSeekerID`.
- Name, age, sex code, work years, and current working state.
- Highest education code, school, and specialty name.
- Current career, expected careers, expected workplace, and expected salary.
- Latest refresh and edit timestamps.
- Most recent employment organization, position label, start date, finish date, and current-job flag.

Do not flatten multiple employment or project rows into duplicate resume results. Do not display generated SQL by default. Display SQL only when the user explicitly requests it or when it is required to explain a query failure.

## Components

### Skill Instructions

`SKILL.md` defines trigger phrases, the natural-language-to-SQL workflow, ambiguity handling, mandatory predicates, result formatting, and the command used to invoke the executor. Keep detailed table schemas in `references/schema.md`.

### Query Executor

`scripts/search_resumes.py` uses the ClickHouse HTTP interface and Python standard-library networking so installation does not require third-party packages. It reads the private connection configuration, sends a single parameterized query, parses `JSONEachRow`, and emits structured JSON.

Before transmission, the executor must:

- Accept only one statement beginning with `SELECT` or `WITH` and containing a `SELECT`.
- Reject semicolons and mutation, DDL, privilege, system, attachment, or optimization statements.
- Restrict table references to the approved resume tables.
- Require a positive result limit.
- Set ClickHouse `readonly=1` and the response format, and enforce a client-side request timeout because this read-only user cannot change server session settings.
- Avoid logging credentials or placing them in error messages.

The read-only database account and ClickHouse HTTP GET restrictions provide independent server-side protection in addition to client validation.

### Connection Configuration

The public `connection.example.json` contains placeholders only. The local `connection.json` contains the actual connection values requested by the owner. The skill must fail with a concise configuration error if the private file is absent or malformed.

## Data Flow

1. Parse the user's Chinese search request into explicit filters, keywords, ordering, and result count.
2. Resolve clear conditions to documented columns and identify ambiguous enum values.
3. Ask one focused clarification when a required mapping is ambiguous; otherwise build parameterized SQL.
4. Validate the SQL and mandatory search policy in the executor.
5. Execute through ClickHouse HTTP in read-only mode and parse `JSONEachRow`.
6. Return exactly the requested number of available summaries, or state the smaller number actually found.

## Errors And Limits

- Distinguish connection, authentication, timeout, invalid SQL, missing configuration, and empty-result errors.
- Never retry authentication or invalid-query failures automatically.
- Use a bounded connection and execution timeout.
- For output too large for chat, preserve all requested rows in a JSONL artifact rather than silently truncating them.
- Never include the password in command output, exceptions, logs, committed files, or generated artifacts.

## Distribution

Publish the skill at `skills/clickhouse-resume-search` so Codex can install it from the GitHub repository path. Keep credentials out of Git history. Recipients create their own `connection.json`; shared credentials, if intentionally reused, must be transferred outside GitHub.

## Verification

Use test-first development for the executor. Cover:

- Default and explicit result limits.
- Mandatory resume predicates and default ordering.
- `ResumeGuid` presence.
- Parameter binding and special-character handling.
- Broad keyword conditions without duplicate resumes.
- Rejection of multi-statement and non-read-only SQL.
- Missing or malformed private configuration.
- HTTP, authentication, timeout, and ClickHouse error handling without credential leakage.
- Parsing `JSONEachRow` and producing the summary schema.

Run the skill validator, unit tests, a read-only live smoke query, and a secret scan before installation or publication.
