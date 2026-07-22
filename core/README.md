# Core migration (Projects, Teams & membership, Alert rules)

The delivered P0 minimum. Five export-driven scripts that recreate a subset of a self-hosted org in SaaS.
They read a self-hosted **export** and POST to `https://sentry.io/api/0`; they do not call the self-hosted API.

- **Dependencies:** `requests` (`pip install "requests>=2.31.0"`).
- **Dry-run is the DEFAULT.** Every script logs the exact method/URL/payload and makes no changes unless you
  pass `--run_on_real_data=true`. (`--dry-run` is accepted everywhere as an explicit no-op.) This matches the
  `project-settings` tool, so every tool in the toolkit shares one convention: **dry by default,
  `--run_on_real_data=true` to apply.** **Always run once in dry-run first and read the output.**
- **One source org per run.** An export file may contain many orgs. Every script takes `--source-org <slug>`
  to select which source org's records to migrate, and will refuse to run on a multi-org export if it's omitted
  (it prints the available org slugs). Run the suite once per source org, in a deliberate order.
- **Outputs never overwrite.** Result/mapping files are tagged `<source-org>_<dest-org>_<timestamp>.json`; each
  script logs the exact path it wrote, and producers name the file to hand to the next step.
- Null padding entries in the exports are skipped automatically.
- **Tests:** `python3 -m unittest discover -s tests` (hermetic; see [`../tests/`](../tests/)).

## Run order (hard dependencies) — per source org

```
0. [SaaS, manual] create a team whose slug is exactly "migration"
1. create_sentry_projects.py   -> projects (created UNDER the "migration" team; original slug preserved)
2. create_sentry_teams.py      -> real teams + attach to already-created projects
                                  writes: project_team_sync_results_<tag>.json (team_id_mappings)
3. add_sentry_members.py       -> org members
                                  writes: user_mappings_for_teams_<tag>.json, member_id_mappings_<tag>.json
4. assign_team_members.py      -> member<->team assignments
                                  reads:  user_mappings_for_teams_<tag>.json (from step 3)
5. migrate_alert_rules.py      -> metric alert rules
                                  reads:  project_team_sync_results_<tag>.json (from step 2)
```

Why this order: projects are POSTed to `/teams/{org}/migration/projects/`, so the manually-created `migration`
team must exist first; the real teams (step 2) attach to already-created projects; member→team assignment needs
the user mappings from step 3; alert rules map an `owner` team via the mappings from step 2.

## Per-script detail (run from the repo root)

### 1. create_sentry_projects.py

- `python3 core/create_sentry_projects.py <auth_token> <dest_org_slug> <export.json> --source-org <src_slug> [--delete] [--run_on_real_data=true]`
- Reads `sentry.project` for the source org; POSTs `{name, slug, platform}` to `/teams/{org}/migration/projects/`.
  The original slug **is** sent, so SaaS preserves it. `--delete` removes by slug (handy for resetting a test org).
- Writes `project_management_results_<tag>.json`.

### 2. create_sentry_teams.py

- `python3 core/create_sentry_teams.py <auth_token> <dest_org_slug> <export.json> --source-org <src_slug> [--run_on_real_data=true]`
- Reads `sentry.team`, `sentry.project`, `sentry.projectteam` for the source org; POSTs teams (with original slug)
  then attaches them to projects.
- Writes `project_team_sync_results_<tag>.json` (includes `team_id_mappings`, consumed by the alert-rule script).

### 3. add_sentry_members.py

- `python3 core/add_sentry_members.py <auth_token> <dest_org_slug> <export.json> --source-org <src_slug> [--test you@gmail.com] [--send-invite] [--run_on_real_data=true]`
- Delete mode: `python3 core/add_sentry_members.py <auth_token> <dest_org_slug> --delete <member_id_mappings_<tag>.json>`
- Reads `sentry.organizationmember` (active users) for the source org; POSTs to `/organizations/{org}/members/` with `orgRole: "member"`.
- Writes `member_id_mappings_<tag>.json` and `user_mappings_for_teams_<tag>.json` (used by step 4). Run step 4 for
  this source org before starting the next org.
- `--send-invite` sets `sendInvite`/`reinvite` (default off = provision without emailing; integration tokens
  may not deliver emails — resend from the UI if needed). `--test` rewrites emails to a `+alias` for safe testing.
- Limitation: role is hardcoded to `member`.

### 4. assign_team_members.py

- `python3 core/assign_team_members.py <auth_token> <dest_org_slug> <export.json> <user_mappings_for_teams_<tag>.json> --source-org <src_slug> [--run_on_real_data=true]`
- Reads `sentry.organizationmemberteam` (+ `sentry.team`) and the user mappings; POSTs
  `/organizations/{org}/members/{member_id}/teams/{team_slug}/`. Writes `team_member_assignments_<tag>.json`.

### 5. migrate_alert_rules.py

- `python3 core/migrate_alert_rules.py <auth_token> <org_slug> <export.json> <project_team_sync_results.json> [--dry-run] [--skip-issue-alerts]`
- Migrates **both** alert types (use `--skip-issue-alerts` for metric-only). Writes
  `alert_rule_migration_results_<timestamp>.json` with separate `metric` and `issue` result sections.
- **Metric alerts** (`sentry.alertrule`, + `snubaquery`, `alertruleprojects`, `alertruletrigger`,
  `snubaqueryeventtype`) → POST `/organizations/{org}/alert-rules/`. Translates: project-slug mapping,
  `queryType` from the snuba type, `eventTypes` from `snubaqueryeventtype`, `timeWindow` seconds→minutes,
  real trigger thresholds, owner → `team:<id>`, and a default email action (SaaS rejects a trigger with no action).
- **Issue alerts** (`sentry.rule`) → POST `/projects/{org}/{project}/rules/`. Carries over the rule's
  `conditions`/`filters`/`actionMatch`/`filterMatch`/`frequency` and maps its environment name; the notification
  **action is defaulted** to email the owner team (`targetType:Team`), falling back to `IssueOwners`/`ActiveMembers`
  when the rule has no owner team (original notification actions aren't portable across instances).
