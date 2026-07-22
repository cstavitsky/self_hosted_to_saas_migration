# Project settings migration

`migrate_project_settings.py` — migrates whitelisted **per-project settings** from a relocation
**export file** to a SaaS org. Fully offline on the source side: it parses `export.json`, so **no
self-hosted instance or self-hosted token is required**.

- **Dependencies:** `requests` (`pip install "requests>=2.31.0"`).
- **SaaS scope:** `project:write` (also used for the inbound-filter PUTs).
- **Project matching:** by **name** (case-insensitive). Core migration reassigns slugs on create but
  preserves names, so name is the stable key. Assumes a greenfield destination.
- Verifies each field/filter with a SaaS GET-back and writes `project_settings_migration_results.json`.

## What it migrates

Everything is sourced from the project's `sentry.projectoption` rows in the export.

1. **General settings** → project-detail `PUT`: `resolveAge` (`sentry:resolve_age`), `allowedDomains`
   (`sentry:origins`), `scrapeJavaScript`, `verifySSL`, `subjectPrefix`/`subjectTemplate`,
   `defaultEnvironment`, `highlightTags`, `highlightContext`.
2. **Custom grouping rules** → project-detail `PUT`: `groupingEnhancements` and `fingerprintingRules`.
   These only affect how *future* events group (no re-grouping/forking of existing issues) and SaaS
   validates their syntax on `PUT`. The grouping *algorithm version* (`sentry:grouping_config`) is
   intentionally **not** migrated — SaaS should keep its current default, and pinning an old version
   risks forking issues into duplicates.
3. **Standard data scrubbers** → project-detail `PUT`: `dataScrubber`, `dataScrubberDefaults`,
   `sensitiveFields`, `safeFields`, `scrubIPAddresses`, `storeCrashReports`. Advanced custom-PII
   (`relayPiiConfig` / trusted relays) is excluded (recorded, not dropped). Org-level scrubbers are out
   of scope — org options aren't carried by the export.
4. **Inbound filters**: the five toggle filters (`browser-extensions`, `legacy-browsers`,
   `web-crawlers`, `localhost`, `filtered-transaction`) via the dedicated
   `/projects/{org}/{slug}/filters/{id}/` endpoint (one `PUT` each, `subfilters` for
   `legacy-browsers`), plus the custom **error-message** filter (`sentry:error_messages`) written on
   the project-detail `options` blob as `filters:error_messages`.

Skipped: project security token, grouping algorithm version, secondary-grouping, built-in symbol
sources, dynamic-sampling biases (see `SKIPPED_OPTIONS` in the script). Every option present on a
source project is accounted for in the results file (`applied` / `filters_applied` /
`excluded_advanced` / `skipped` / `unhandled`) — nothing is silently dropped.

## Export-only caveat

A relocation export carries `sentry.projectoption` rows for **non-default values only**. A filter or
setting the customer never changed has no row, so it's left at the **SaaS default** on the
destination — we can't normalise it without the self-hosted defaults table. (This is the trade-off
vs. a live-API reader, which returns every field's effective value.) For inbound filters this
matters because SaaS turns some on by default for new projects: an explicitly-disabled filter *is*
carried over (it has a `"0"` row), but a never-touched filter inherits the SaaS default.

## Run

```bash
python3 project-settings/migrate_project_settings.py "$SAAS_TOKEN" "$DEST_ORG" \
    --export-file /path/to/export.json \
    [--source-org <slug>] \
    [--saas-url https://sentry.io/api/0] \
    [--run_on_real_data=true]
```

**Runs dry by default** (logs the intended `PUT`s without sending them); add **`--run_on_real_data=true`** to apply.
Review the dry-run output first. Output is formatted as aligned key/value blocks per project plus a
summary table.

**Multi-org exports:** an export file can contain several orgs (e.g. a consolidation). By default all
projects across all orgs in the file are processed and matched by name into `$DEST_ORG`. Use
`--source-org <slug>` to restrict to a single source org. When multiple source orgs share a project
name, run the pre-flight `../preflight/duplicates_report.py` first — match-by-name will otherwise map
same-named projects onto the same destination project.
