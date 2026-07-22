# Changelog

All notable changes to the migration scripts, relative to the upstream baseline
[dgbailey/migration](https://github.com/dgbailey/migration) (commit `2bd3bf6`).

Format loosely follows [Keep a Changelog](https://keepachangelog.com/). This project uses the
upstream fork's own history, not semver releases; the core-scope checkpoint is tagged `v1.0-core`.

## [Unreleased] - settings made export-only; org-level removed

Consolidated the settings migration to be **100% export-driven** and scoped **out** org-level settings.
See DECISIONS.md **D9**.

### Added

- `common/export_source.py` (new): shared read-only parser for a relocation export. Builds per-project
  `sentry.projectoption` dicts, decoding the export's mixed native/JSON-encoded option values.
- `migrate_project_settings.py`: now migrates, from the export, **custom grouping rules**
  (`groupingEnhancements`, `fingerprintingRules`), **standard project-level data scrubbers** (folded in
  from the old data-scrubbers tool), the **custom error-message filter**, and the **five toggle inbound
  filters** (via the dedicated `/filters/` endpoint), each replicated to its exact state. Per-project
  accounting: `applied` / `filters_applied` / `excluded_advanced` / `skipped` / `unhandled`.

### Changed

- `migrate_project_settings.py` is now **export-driven** (`--export-file`) instead of live-API. Dropped
  `--source-token` / `--source-url`; `--source-org` is now an optional filter for multi-org export files.
  Grouping algorithm *version* (`sentry:grouping_config`) remains intentionally skipped.

### Removed

- `org-settings/` (`migrate_org_settings.py`) — **org-level settings are out of scope** (org options
  aren't reliably carried by the export).
- `data-scrubbers/` (`migrate_data_scrubbers.py`) — project-level scrubbers folded into
  `migrate_project_settings.py`; org-level scrubbers dropped with the rest of org-level scope.
- `common/selfhosted_source.py` — the live self-hosted reader; no tool uses a live API anymore, so no
  self-hosted token or network reachability to the instance is required for any step.

## [v1.0-core] - 2026-07-08

Core-scope migration hardened and verified end-to-end (Projects, Teams & Membership, Alert Rules)
into the SaaS test org `dorian-v25-migration`.

### Added

- `--dry-run` **on all five migration scripts** (`create_sentry_projects.py`, `create_sentry_teams.py`,
`add_sentry_members.py`, `assign_team_members.py`, `migrate_alert_rules.py`). Logs the exact
method / URL / payload each would send, and returns fake ids/slugs so downstream steps can be
previewed too, without touching SaaS.
- `--send-invite` **flag on** `add_sentry_members.py`**.** Controls `sendInvite`/`reinvite` (default off,
preserving the original bulk-provision-without-email behavior). When set, the API attempts to send
invitation emails.
- `check_duplicates.py` **(new).** Offline pre-flight that scans one or more exports and reports
team/project **slug** collisions (would break a merged live run) and **name** collisions
(informational). Writes `duplicate_report.json`; exits non-zero on slug collisions. Never calls SaaS.
- `requirements.txt` **(new).** Pins the only dependency (`requests`).
- `README.md` **(new).** Annotated repo index: what each script does, run order, dependencies, known
limitations, token/permission notes.
- `ROADMAP.md` **(new).** Scope targets, milestones, and branch model.
- `.gitignore` **(new/real).** Ignores `__pycache__/`, `.venv/`, and runtime artifacts
(`export*.json`, `*_mappings.json`, results JSON, `duplicate_report.json`, `dryrun-out/`).
- `docs/` **(new).** Self-hosted setup runbook (`phase-1`) and migration runbook (`phase-2`).



### Changed

- `migrate_alert_rules.py` **- near rewrite** (~167 insertions / ~139 deletions). The original was an
unfinished scaffold that would fail on the first real rule. Now:
  - Real project targeting via `sentry.alertruleprojects` -> `sentry.project` slug (removed the
  hardcoded `"projects": ["your-project-slug"]` placeholder).
  - `queryType` taken from the snuba `type` field (was mistakenly the query string); the actual query
  string is now sent as its own `query` field (previously omitted).
  - `eventTypes` derived from `sentry.snubaqueryeventtype`.
  - `timeWindow` converted from seconds (self-hosted) to minutes (SaaS).
  - Real trigger labels/thresholds read from `sentry.alertruletrigger` (were hardcoded defaults from a
  non-existent field).
  - Owner mapped from the rule's `team` field -> new SaaS team id, formatted `team:<id>`.
  - Default **email-to-owner-team action injected** into any trigger with no action, since SaaS rejects
  a trigger with empty `actions` while self-hosted allows it and the export carries none.
  - Issue alerts (`sentry.rule`) are detected and reported as `skipped_issue_alerts` instead of being
  silently ignored.
  - Per-rule O(n) export scans replaced with index dicts built once.
  - Results written as structured `{migrated, failed, skipped_issue_alerts}` with counts.
- `create_sentry_projects.py`**.** Added a `slugify()` helper to predict the SaaS-derived slug in
dry-run output; platform fallback now handles null/empty values (`fields.get('platform') or 'python'`),
not just a missing key.
- `create_sentry_teams.py`**.** Migrated CLI from positional `sys.argv` parsing to `argparse`
(named args + `--help`).
- `assign_team_members.py`**.** Migrated CLI from positional `sys.argv` parsing to `argparse`.



### Fixed

- `migrate_alert_rules.py`**:** guarded access to `e.response.text` in the error handler, which
previously raised `AttributeError` on non-HTTP exceptions and masked the real error.



### Removed

- `keep.txt`**.** Dustin's scratch scope list; folded into `ROADMAP.md`.



### Known limitations (carried, flagged for review)

- Issue alerts (`sentry.rule`) are not migrated (metric alerts only).
- Alert notification actions are not preserved (a default action is injected).
- Member roles are flattened to `member` at invite time (integration-token limitation).
- Project slugs / DSNs change because slug isn't sent on create.
- Duplicate names across merged instances must be resolved manually (`check_duplicates.py` reports them).



## [Unreleased]

Repo restructured around a `main` trunk with one `feat/<data-type>` branch + PR per remaining data
type (see `ROADMAP.md`).

### Added (feat/issue-alerts)

- `migrate_alert_rules.py` now migrates **issue alerts** (`sentry.rule`) in addition to metric alerts,
  POSTing to `/projects/{org}/{project}/rules/`. It carries over each rule's
  `conditions`/`filters`/`actionMatch`/`filterMatch`/`frequency` and maps the environment name; the
  notification **action is defaulted** to email the mapped owner team (`targetType:Team`), falling back to
  `IssueOwners`/`ActiveMembers` when a rule has no owner team (see DECISIONS D9).
- `--skip-issue-alerts` flag on `migrate_alert_rules.py` (metric-only behavior).
- Results file now has separate `metric` and `issue` sections (`{migrated, failed}` each); the completion
  log reports both counts.
- `tests/` **(new).** First hermetic unit tests (`tests/test_issue_alerts.py`, 15 cases) — stub `requests`,
  run with plain `python3 -m unittest discover -s tests`, no network. Cover the issue-alert action defaulting,
  owner-team mapping + IssueOwners fallback, condition/filter/env/frequency handling, endpoint, error paths,
  dry-run, and the `--skip-issue-alerts` flag.
- `--only NAME` flag on `migrate_alert_rules.py` (repeatable). Migrates only the alerts (metric or issue)
  whose name/label matches exactly — handy for surgical single-alert re-tests. When omitted, behavior is
  unchanged (all alerts).

### Added (experiments/ — Slack integration carry-over spike)

- `experiments/slack_action_carryover.py` **(new, experimental).** Proves that an issue alert's **Slack
  notification action can survive migration** when the same Slack workspace is already installed on the
  destination SaaS org — instead of the default email substitution. It reads the alert + Slack action from
  the export, looks up the destination Slack integration id via the live SaaS API, rewrites only the
  instance-specific `workspace` field (keeping `channel`/`channel_id`), and POSTs the rule (polling SaaS's
  async channel-validation task). Verified live end-to-end. Not wired into the supported toolkit yet; a
  future `--preserve-integrations` flag on `migrate_alert_rules.py` would productionize it.

### Docs

- Renamed the destination-org env var `ORG` -> `DEST_ORG` across the README and settings-folder READMEs, to
  read clearly alongside `SRC_ORG` (source) in a merge.
- `SOURCING.md` **(new).** Explains that the export and the live self-hosted API are used in **separate
  steps** (pre-flight/core = export; settings = live API), with a per-step source table, and documents how
  to produce the export on managed/dedicated hosting (Step 0 variant c).
- Removed `requirements.txt`; the sole dependency is now installed inline (`pip install "requests>=2.31.0"`)
  in the README and each tool's folder README.
- `README.md`: turned the master runbook into a full command-level guide -- a "set once" env-var block,
  the exact dry-run/live command for every script in order (Step 0 export -> Step 1 duplicates -> Step 2
  prereqs -> Step 3 core -> Step 4 settings), and a multi-org-merge "repeat per source org" note.
- Documented **hosting-agnostic** operation: Step 0 export shown three ways (host CLI / local Docker /
  provider hand-off), and the settings steps take `--source-url "$SRC_URL"` to target any self-hosted
  instance (not just local Docker), with the read token minted on that instance. The three settings folder
  READMEs now call out `--source-url` and reachability explicitly.

### Added (feat/org-settings)

- `selfhosted_source.py` (new): read-only live client for the self-hosted Sentry API (auth header,
  RFC5988 cursor pagination, `get_org`). The second data source, for models the relocation export
  does not carry. Reused and extended by later features.
- `migrate_org_settings.py` (new): migrates organization governance + privacy settings from the live
  self-hosted org to SaaS via a whitelist copy (`PUT /organizations/{org}/`). Includes `--dry-run`,
  post-run verification (GET-back compare), and a results file. Data-scrubbing fields are deferred to
  `feat/data-scrubbers` and `require2FA` is intentionally skipped -- both are recorded in the results
  file rather than silently dropped.

### Added (feat/project-settings)

- `selfhosted_source.py`: added `get_projects(org_slug)` (paginated project list) and
  `get_project(org_slug, project_slug)` (full per-project settings) helpers.
- `migrate_project_settings.py` (new): migrates per-project general settings from the live self-hosted
  org to SaaS. **Greenfield** scope: pairs source -> destination projects by **name** (case-insensitive,
  since phase-2 reassigned slugs but preserved names) and PUTs to the destination slug; unmatched source
  projects are skipped and reported. Whitelist (`resolveAge`, `allowedDomains`, `scrapeJavaScript`,
  `verifySSL`, `subjectPrefix`, `subjectTemplate`, `defaultEnvironment`, `highlightTags`,
  `highlightContext`). Data-scrubbing fields deferred to `feat/data-scrubbers`; identity/advanced/risky
  fields skipped -- both recorded per project. Includes `--dry-run`, per-project GET-back verification,
  and a `project_settings_migration_results.json` results file. Needs a SaaS `project:write` token.
- `migrate_project_settings.py`: human-readable run output -- dropped the logger prefix, one aligned
  per-project block (source/dest, `key = value` settings, deferred summary, action, verify) and a final
  summary table. Output only; behavior and results file unchanged.
- `ROADMAP.md`: marked org-settings and project-settings done; added a future `feat/collision-preflight`
  hardening milestone for brownfield destinations (pre-flight collision report + per-type merge policy +
  provenance).

### Added (feat/data-scrubbers)

- `migrate_data_scrubbers.py` (new): migrates the **standard** data-scrubbing settings deferred by the
  two settings features, at **both** org and project level, from the live self-hosted instance to SaaS.
  Whitelist (`dataScrubber`, `dataScrubberDefaults`, `sensitiveFields`, `safeFields`, `scrubIPAddresses`,
  `storeCrashReports`). Org via `PUT /organizations/{org}/`; projects paired by name (reusing the
  project-settings matching) via `PUT /projects/{org}/{proj}/`. `--org-only` / `--projects-only` scope
  flags, `--dry-run`, per-target GET-back verification, and a `data_scrubbers_migration_results.json`
  results file. The advanced custom-PII fields `relayPiiConfig` and `trustedRelays` are intentionally
  excluded (recorded, not dropped) -- see `DECISIONS.md` (D5). Needs a SaaS `org:write` + `project:write`
  token.
- `DECISIONS.md` (new): running log of scope/design choices we may revisit (advanced scrubbers deferral,
  project match-by-name/greenfield, `require2FA` skip, member-role flattening, metric-alerts-only).

### Added (feat/duplicates-report)

- `duplicates_report.py` (new): the migration suite's first tool -- a cross-org duplicates / collision
  report for the multi-org consolidation case (several self-hosted orgs -> one SaaS org). Reads one JSON
  export per org and reports **project-name** collisions (HARD; SaaS derives the slug from the name),
  **team-slug** collisions (HARD; slug must be unique), **team-name** collisions with a per-org
  **membership diff** (same team name, different rosters), plus **project-slug** collisions and
  **similar org names** (informational). Writes `duplicate_report.json`; exits non-zero on HARD
  collisions. Offline / export-based only (no live instance) -- see `DECISIONS.md` (D7). Optional
  `--label PATH=Name` and `--similarity` flags.
- `DECISIONS.md` (D7): duplicates report is export-based/offline for now; a live multi-org reader and
  usage/volume-based prioritization are deferred.
- `duplicates_report.py`: `--html [PATH]` flag -- also writes a **self-contained** `duplicate_report.html`
  (inline CSS, no server/dependencies, opens offline) with severity-colored sections, org cards, and the
  per-team membership diff. HTML output is gitignored; JSON output/exit codes are unchanged.
- `duplicates_report.py`: renamed the human-facing severity label **`HARD` -> `Danger`** (with `Info`) in
  the HTML and console output, and added a **severity reference legend** to the HTML report. The roster-diff
  badge is neutral gray (red stays exclusive to Danger, amber to Info).
- `duplicates_report.py`: project collision detection now works on the **derived slug** (`slugify(name)`),
  which is what SaaS generates on create -- merging the former separate "project name" (Danger) and
  "project slug" (info) checks into one accurate Danger check that also catches different names that
  slugify to the same value. Removed the redundant source-slug section. JSON key is now
  `project_collisions_HARD` (each entry carries `derived_slug`); summary uses `project_collisions`.

### Changed (repo restructure + anonymization)

- **Repository restructured into per-tool subfolders**, each with its own run-guide `README.md`:
  `common/` (`selfhosted_source.py`), `preflight/` (`duplicates_report.py`), `core/` (the five phase-2
  scripts), `org-settings/`, `project-settings/`, `data-scrubbers/`. Moved via `git mv` (history preserved).
- The three settings tools gained a small `sys.path` shim so they import `common/selfhosted_source.py`
  while staying runnable directly from the repo root.
- Top-level `README.md` rewritten as a suite index (data-flow, ordered tool table, token/permission notes,
  dependencies, known limitations) linking into each subfolder's README; `ROADMAP.md` gained a repository
  layout section.
- `.gitignore`: consolidated the per-file results rules into `*_migration_results.json` (also covers
  `member_roles_migration_results.json`, which had held real emails while untracked).

### Removed

- `check_duplicates.py`: subsumed by `duplicates_report.py`, which covers the same slug/name collisions
  plus team-membership diffs, org-name similarity, and a HARD-vs-informational distinction.
- `docs/` (setup + migration runbooks) removed from the published repo — the only tracked files that
  carried a customer name. Reference copies are retained locally under the project's `reports/` folder.
- Stray no-extension `create_sentry_projects` duplicate (older broken variant).

