# Sentry Self-Hosted → SaaS Migration Toolkit

**This README is the master runbook.** The migration is performed by running the tools below **one at a
time, in the order given** — there is deliberately **no single orchestrator script**. Each tool does one
data type, **runs dry by default** (pass `--run_on_real_data=true` to apply), and writes its own results file that you review **before** running the next
one. Do not skip ahead: later steps depend on files written by earlier ones, and the manual review between
steps is the safety checkpoint against destructive writes (see [DECISIONS.md](DECISIONS.md) D8).

Fork of [github.com/dgbailey/migration](https://github.com/dgbailey/migration), reorganized into per-tool
folders.

- **Roadmap, milestones, branch model:** [ROADMAP.md](ROADMAP.md)
- **Changelog (vs. upstream):** [CHANGELOG.md](CHANGELOG.md)
- **Scope decisions log (deferrals to revisit):** [DECISIONS.md](DECISIONS.md)
- **Producing the export:** [SOURCING.md](SOURCING.md)

## How the data flows

The migration is **100% export-driven**. A self-hosted relocation export (Django `dumpdata`-style JSON —
a flat list of `{"model", "pk", "fields"}` objects) is parsed and recreated/applied in SaaS. No step reads
a live self-hosted API, so no self-hosted token or network reachability to the instance is required.

```
self-hosted --(export)-->  export.json  --(tools + SaaS token)--> sentry.io
```

Every step (pre-flight, core, settings) reads only the export file and writes only to SaaS. See
[SOURCING.md](SOURCING.md) for how to produce the export on managed/dedicated hosting.

**Org-level settings are out of scope.** Organization governance / org-level scrubbing defaults live in
`sentry.organizationoption`, which the relocation export does not reliably carry, so they are intentionally
not migrated by this toolkit.

## Set these once (referenced by every command below)

```bash
export SAAS_TOKEN=...          # SaaS internal-integration token
export DEST_ORG=my-saas-org    # destination SaaS org slug
export SRC_ORG=migration-test-org   # source org slug (optional filter for multi-org export files)
export EXPORT=export.json       # path to the export for the org currently being migrated
```

Tokens: create the **SaaS token** as an Internal Integration (Org Settings → Developer Settings → Custom
Integrations) with scopes `org:write team:write project:admin member:write alerts:write` (member invites also
require a plan with the invite feature enabled). No self-hosted token is required — the migration is
export-driven. No credentials are ever committed or shipped.

### Works with any self-hosted hosting (dedicated host, VM, or local Docker)

The tools never connect to the self-hosted instance — they only consume the export file it produces. The one
thing that varies by hosting is **how you produce the export (Step 0)**; see the three variants below.

## Order of operations (run each step, review, then continue)

Run every command from this directory. **Every tool runs dry by default** — run it as-is, inspect the
output, then re-run the same command with **`--run_on_real_data=true`** to apply the changes to SaaS.
(`--dry-run` is accepted everywhere as an explicit no-op if you like to be verbose.) After each step,
review the results file it writes before moving on. Each folder's own `README.md` has the full per-flag detail.

### Step 0 — Produce the self-hosted export (one JSON per source org)

Use whichever matches your access; all three produce the same relocation JSON:

```bash
# a) shell/CLI on the self-hosted host
sentry export organizations export.json --filter-org-slugs "$SRC_ORG" --no-prompt

# b) local Docker Compose (mount a host dir so the file lands outside the container)
docker compose run --rm -T -v "$PWD:/export" \
  web export organizations /export/export.json --filter-org-slugs "$SRC_ORG" --no-prompt

# c) managed/dedicated hosting: have the provider/admin run the relocation export and hand you the JSON
#    (see SOURCING.md for exactly what the provider needs to run and how to receive the file)
```

For a multi-org merge, run Step 0 once per source org (`--filter-org-slugs <slug>`) to get `org1.json`,
`org2.json`, …

### Step 1 — Pre-flight duplicates report (multi-org merges only)

Offline and read-only; never writes to SaaS. Resolve every **Danger** collision (rename/merge/drop) before
migrating anything.

```bash
python3 preflight/duplicates_report.py org1.json org2.json org3.json --html
# review duplicate_report.html / duplicate_report.json
```

### Step 2 — Destination prerequisites (before any write)

Install the one dependency (`requests`) used by the core and project-settings tools (`preflight/` needs nothing):

```bash
pip install "requests>=2.31.0"
```

Then, in SaaS: confirm the destination org exists, and create a **team whose slug is exactly `migration`**
(every project is created under it in Step 3a). Make sure `SAAS_TOKEN`, `DEST_ORG`, and `EXPORT` are exported
(above).

### Step 3 — Core content (projects → teams → members → membership → alerts)

Order is fixed by mapping-file dependencies. Each command below runs **dry by default**; append
**`--run_on_real_data=true`** to the same command to apply it once you've reviewed the dry-run output.

```bash
# 3a. Projects  -> project_management_results.json
python3 core/create_sentry_projects.py "$SAAS_TOKEN" "$DEST_ORG" "$EXPORT"

# 3b. Teams (+ attach to projects)  -> project_team_sync_results.json
python3 core/create_sentry_teams.py "$SAAS_TOKEN" "$DEST_ORG" "$EXPORT"

# 3c. Members  -> user_mappings_for_teams.json, member_id_mappings.json
python3 core/add_sentry_members.py "$SAAS_TOKEN" "$DEST_ORG" "$EXPORT"

# 3d. Team membership  -> team_member_assignments.json
python3 core/assign_team_members.py "$SAAS_TOKEN" "$DEST_ORG" "$EXPORT" user_mappings_for_teams.json

# 3e. Alerts (metric + issue)  -> alert_rule_migration_results_<ts>.json
python3 core/migrate_alert_rules.py "$SAAS_TOKEN" "$DEST_ORG" "$EXPORT" project_team_sync_results.json
```

Add `--run_on_real_data=true` to any of these to actually write, e.g. `... create_sentry_projects.py "$SAAS_TOKEN" "$DEST_ORG" "$EXPORT" --run_on_real_data=true`.

### Step 4 — Project settings (export-driven)

A single tool applies all per-project settings from the export: general settings, custom grouping rules
(`groupingEnhancements` / `fingerprintingRules`), standard data scrubbers, the custom error-message filter,
and the five toggle inbound filters. Org-level settings are out of scope (see above).

```bash
# 4. Per-project settings + scrubbers + inbound filters  -> project_settings_migration_results.json
#    Dry by default; add --run_on_real_data=true to apply.
python3 project-settings/migrate_project_settings.py "$SAAS_TOKEN" "$DEST_ORG" \
  --export-file "$EXPORT" [--source-org "$SRC_ORG"] [--run_on_real_data=true]
```

### Multi-org merge

For N source orgs merging into one SaaS `$DEST_ORG`: run Step 1 once across all exports, then repeat Steps 3–4
once per source org — set `EXPORT` to that org's export each time (or use one combined export and pass
`--source-org` where a step supports it).

Why the order is fixed (hard dependencies): a SaaS team slugged `migration` must exist before **3a**; **3b**
attaches teams to already-created projects; **3d** needs the user mappings from **3c**; **3e** maps each
alert's owner team via the mappings from **3b**. Settings (Step 4) run after the objects they configure exist.
See [`core/README.md`](core/) for the full dependency notes.

Shared code lives in [`common/`](common/) (`export_source.py`, the relocation-export parser, and
`run_logging.py`).

## Dependencies

- `preflight/` — **none** (Python 3 standard library only).
- `core/` and `project-settings/` — `requests`:

```bash
pip install "requests>=2.31.0"
```

## Known limitations (carried, flagged for review)

- **Alert notification actions** are not preserved; migrated rules (metric and issue) get a default
  email-the-owner-team action only (issue alerts fall back to `IssueOwners` when a rule has no owner team).
- **Member roles** are flattened to `member` (integration-token invite limitation).
- **Project slugs / DSNs change** because slug isn't sent on create (SaaS derives it from the name).
- **Duplicate names across instances** must be resolved before a merged run (`preflight/duplicates_report.py`
  reports them; the tools do not auto-rename).
