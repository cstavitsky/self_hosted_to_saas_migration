# Migration Roadmap

Goal: migrate a full Sentry organization from **self-hosted v25 -> Sentry SaaS**. The core scope
(Projects, Teams & Membership, Alert Rules) is complete and frozen at tag `v1.0-core`. Remaining data
types are delivered one at a time as small feature branches merged into `main` via PR.

## Branch model

- `main` -- integration trunk, started from the `v1.0-core` checkpoint. Default branch. Everything is
  merged here via PR.
- `master` -- pristine mirror of upstream `dgbailey/migration`; used only to pull Dustin's updates
  (`git fetch upstream`). Never developed on.
- `phase-2-core` + tag `v1.0-core` -- the frozen checkpoint. Never modified.
- `feat/<data-type>` -- one branch per data type below, one PR into `main`.

```mermaid
flowchart TD
  upstream["upstream/master = Dustin (pristine)"]
  main["main = trunk (from v1.0-core)"]
  upstream -.->|fetch when needed| main
  main --> reader["feat/selfhosted-source (first)"]
  reader --> main
  main --> feat["feat/<data-type> (one PR each)"]
  feat -->|PR| main
```

## Repository layout

The toolkit is organized into per-tool subfolders, each with its own run-guide `README.md`:

```
migration/
  README.md ROADMAP.md DECISIONS.md CHANGELOG.md SOURCING.md
  common/            export_source.py                (shared relocation-export parser)
                     run_logging.py                  (shared run-log helper)
  preflight/         duplicates_report.py            (step 0: cross-org collision report)
  core/              create_sentry_projects.py create_sentry_teams.py
                     add_sentry_members.py assign_team_members.py migrate_alert_rules.py
  project-settings/  migrate_project_settings.py     (settings, grouping rules, scrubbers, inbound filters)
```

`project-settings/` imports `common/export_source.py` via a small `sys.path` shim so it stays runnable
directly from the repo root (`python3 project-settings/migrate_project_settings.py ...`).

## One data source: the relocation export

The migration is **100% export-driven** -- everything is parsed from `export.json` (`export organizations`)
via `common/export_source.py`. No tool reads a live self-hosted API, so no self-hosted token is needed.

Confirmed against `migration-testing/export.json`:
- In the export: `sentry.organization` (name, default_role, flags), `sentry.project` +
  `sentry.projectoption` (non-default only) + `sentry.projectkey`, `sentry.team`,
  `sentry.organizationmember` (real `role`), `sentry.rule`.
- NOT in the export: `sentry.organizationoption` (org data-scrubbing defaults, retention),
  `sentry.useroption`, `sentry.dashboard`, `sentry.monitor`, `sentry.repository`, `sentry.savedsearch`.

Because org-level options aren't carried by the export, **org-level settings are out of scope** for this
toolkit (the earlier live-API `org-settings`/`data-scrubbers` org path and `selfhosted_source.py` reader
were removed -- see CHANGELOG). Project-level scrubbers, which *are* in the export, were folded into
`project-settings`.

## Status

Core (done, tagged `v1.0-core`):

- Projects
- Teams & membership
- Alert rules (metric); **issue alerts added post-`v1.0-core` in `feat/issue-alerts`** (DONE — both alert
  types now migrated; issue-alert notification actions defaulted to email the owner team, see DECISIONS.md D9)

Pre-flight (run first, before any migration):

- `feat/duplicates-report` -- **cross-org duplicates / collision report** (DONE). `duplicates_report.py`
  reads one JSON export per self-hosted org and reports project-name collisions (HARD), team-slug
  collisions (HARD), team-name collisions with a **membership diff**, and similar org names. Export-based
  / offline for now (see DECISIONS.md D7). This is the consolidation (export-vs-export) half of the
  collision-preflight idea below.
  - Expected real-world scale for the pilot merge: **Dor-Org1 ~20 projects (high volume, top priority)**,
    **Dor-Org2 ~1000 projects (lower volume, likely unused duplicates)**, Dor-Org3, ... all merging into
    **one** SaaS org. The report is O(n) (dict/set lookups) so this size is trivial to compute; at ~1000+
    projects the console listing gets long, so `duplicate_report.json` is the durable/source-of-truth
    output. A larger-scale test (seed Org2 to hundreds/thousands of projects) is a good follow-up before
    the real run.

Foundation:

- `common/export_source.py` -- shared relocation-export parser that the settings tool depends on
  (replaced the earlier live self-hosted reader).

Milestone: settings

- `feat/project-settings` -- Per-project settings, now export-driven (DONE, merged). Covers general
  settings, custom grouping rules (`groupingEnhancements`/`fingerprintingRules`; grouping *algorithm
  version* excluded), standard project-level data scrubbers (folded in; advanced custom-PII excluded per
  DECISIONS.md D5), the custom error-message filter, and the five toggle inbound filters.
- ~~`feat/org-settings`~~ / ~~`feat/data-scrubbers`~~ -- REMOVED. Org-level settings are out of scope
  (org options aren't in the export); project-level scrubbers were folded into `project-settings`.
- `feat/member-roles` -- User accounts / member options (roles)
- Teams and their settings -- verify-only (teams carry only name/slug/status; no dedicated branch
  unless org-level team roles warrant one)

Milestone: content (future / not built)

> NOTE: the sections below were originally scoped as **live-API sourced** and reference a "reader" that
> has since been **removed** (the toolkit is now export-only). If/when these are built, they will each
> need either a re-introduced live self-hosted reader or an export-based source. Left here as design notes.

- `feat/monitors` -- Crons
- `feat/dashboards` -- Dashboards
- `feat/repos` -- Repositories (integration-gated)
- `feat/saved-searches` -- Saved searches (recent/per-user searches are out of scope)

Delivery model: distinct, separately-run tools (no single auto-orchestrator)

Per supervisor direction (see DECISIONS.md D8), the toolkit ships as **distinct tools the operator runs
one at a time, in a documented order** -- NOT a single `migrate.py` wizard that chains everything. The
reason is overwrite safety: a one-button run makes it too easy to fire a step that mutates the
destination before the operator has reviewed the prior step's output. Each tool:

- does one data type, is independently runnable, and is `--dry-run`-first;
- writes its own results file the operator reviews before running the next tool;
- requires the operator to explicitly pass tokens at run time (no credentials committed or shipped).

Instead of a wizard, delivery = a **README run-order / runbook** that lists the tools in sequence
(pre-flight `duplicates_report.py` -> projects -> teams -> membership -> settings -> scrubbers ->
alerts -> ...), each an explicit, separate command. A thin optional convenience runner may be
reconsidered later, but only as opt-in and never as the default path.

Hardening (future; needed before brownfield customers):

- `feat/collision-preflight` -- today every feature assumes a **greenfield** destination (a fresh SaaS
  org we control). Real customers may migrate into an **existing, in-use** org, where names/slugs can
  collide with objects the customer already relies on. This milestone adds:
  - a `--dry-run` **pre-flight report** per data type ("these already exist in the destination"),
    generalizing `duplicates_report.py` from export-vs-export (already delivered) to
    source-vs-live-destination;
  - a configurable **per-type policy** (`skip` / `rename` / `merge` / `overwrite` / `fail`);
  - **provenance tracking** so re-runs only touch migration-created objects (safe idempotency);
  - a safe default of report-only / skip for org-level and security settings.
  Open question for the customer: are migrations always into a fresh org, or sometimes brownfield? That
  answer decides how much of this we build.

## Feature specs

Every feature reuses the phase-2 mapping files as foreign-key currency
(`project_team_sync_results.json` -> team ids + project slugs, `member_id_mappings.json` /
`user_mappings_for_teams.json` -> member ids) and follows the pattern: `--dry-run`, writes a
`*_results.json`, one new script file, core scripts untouched. Endpoint paths are best-known and to be
confirmed during each build.

### common/export_source.py (foundation)
- File: `export_source.py`. Read-only parser for a relocation export (`{model, pk, fields}` list). Builds
  per-project option dicts (decoding the export's mixed native/JSON-encoded values). Offline; no network.
- The settings tool imports it. Replaced the removed live self-hosted reader (`selfhosted_source.py`).

### feat/project-settings (DONE, export-driven)
- Source: parse the export via `common/export_source.py` -- each project's `sentry.projectoption` rows.
- Matching (greenfield): pair source -> destination by project **name** (case-insensitive), then PUT
  using the destination's own slug (phase-2 reassigned slugs but preserved names). Source projects with
  no name match are skipped and reported; brownfield collision handling is deferred (see below).
- Target: PUT `/projects/{org}/{proj}/` for flat fields, plus PUT `/projects/{org}/{proj}/filters/{id}/`
  for each toggle inbound filter. Whitelist:
  - general: `resolveAge`, `allowedDomains`, `scrapeJavaScript`, `verifySSL`, `subjectPrefix`,
    `subjectTemplate`, `defaultEnvironment`, `highlightTags`, `highlightContext`;
  - grouping rules: `groupingEnhancements`, `fingerprintingRules` (algorithm *version* skipped);
  - standard scrubbers (folded in): `dataScrubber`, `dataScrubberDefaults`, `sensitiveFields`,
    `safeFields`, `scrubIPAddresses`, `storeCrashReports` (advanced `relayPiiConfig`/`trustedRelays`
    excluded, recorded not dropped -- see DECISIONS.md D5);
  - inbound filters: the custom error-message filter + the five toggle filters (browser-extensions,
    legacy-browsers, web-crawlers, localhost, filtered-transaction), replicated to their exact state.
  Every present option is accounted for per project in the results file (applied / excluded / skipped /
  unhandled). Deps: phase-2 projects. Script: `migrate_project_settings.py`. Needs a SaaS `project:write` token.

### feat/member-roles
- Source: export `sentry.organizationmember.role`.
- Target: preserve real role via PUT `/organizations/{org}/members/{member_id}/` `{orgRole}` after the
  invite (needs a `member:admin` token). User notif options (`sentry.useroption`) are not in the export
  and not admin-settable for other users -> documented as self-serve / out of scope.
- Deps: phase-2 members + member id map. Script: `migrate_member_roles.py`.

### feat/monitors (needs reader)
- Source: live GET `/organizations/{org}/monitors/`.
- Target: POST `/organizations/{org}/monitors/` (schedule, checkin_margin, max_runtime, timezone,
  project). Deps: projects. Script: `migrate_monitors.py`.

### feat/dashboards (needs reader)
- Source: live GET `/organizations/{org}/dashboards/` + per-dashboard widgets.
- Target: POST `/organizations/{org}/dashboards/`; remap widget project refs via the phase-2 map.
- Script: `migrate_dashboards.py`.

### feat/repos (needs reader)
- Source: live GET `/organizations/{org}/repos/`.
- Target: POST `/organizations/{org}/repos/` -- gated on the destination org having the source-code
  integration (GitHub/GitLab) installed and authorized. Best-effort/partial; integration prereq
  documented. Script: `migrate_repos.py`.

### feat/saved-searches (needs reader)
- Source: live GET `/organizations/{org}/searches/`.
- Target: POST `/organizations/{org}/searches/`. Recent/per-user searches are out of scope.
- Script: `migrate_saved_searches.py`.

## Acceptance criteria (per feature/PR)

- Dry-run prints the exact intended API call against the seeded/live source.
- After a live run, the SaaS test org (`dorian-v25-migration`) matches source for the whitelisted fields.
- Anything not carried is listed explicitly in the script output and here (no silent drops).
- Merge order respects dependencies: `feat/selfhosted-source` first, then features that import it.

## Working model (do not disrupt the checkpoint)

- The `v1.0-core` tag and the core scripts are frozen; never modified.
- One data type = one `feat/*` branch = one new script file = one PR into `main`. Near-zero conflicts
  because features only add files.
- Reuse the running slim-core self-hosted stack and the `dorian-v25-migration` SaaS test org as-is.
- Pull Dustin's upstream updates via `git fetch upstream` into `master`, then merge into `main` if wanted.
