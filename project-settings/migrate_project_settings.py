"""Migrate per-project settings from a relocation EXPORT -> SaaS (greenfield).

Source of truth is a relocation export file (`export organizations`), NOT a live self-hosted
instance. Per project the migration is: read the project's `sentry.projectoption` rows from the
export, map the whitelisted raw option keys to SaaS API fields, PUT them to the matching
destination project, then verify with a SaaS GET.

This tool covers four things, all applied per project:
  1. General settings -- flat fields on the project detail object (SETTINGS_FIELD_TO_OPTION),
     PUT to /projects/{org}/{slug}/ and verified with a GET.
  2. Custom grouping rules -- `groupingEnhancements` and `fingerprintingRules` (also flat project
     fields). These ARE migrated; `sentry:grouping_config` (the grouping *algorithm version*) is
     intentionally NOT -- SaaS should keep its current default algorithm, and copying an old
     version can fork the issue stream. See SKIPPED_OPTIONS.
  3. Standard data scrubbers (project level) -- dataScrubber, dataScrubberDefaults, sensitiveFields,
     safeFields, scrubIPAddresses, storeCrashReports (also flat project fields). Advanced custom-PII
     (relay_pii_config / trusted relays) stays excluded -- see EXCLUDED_ADVANCED_OPTIONS.
  4. Inbound filters -- the custom error-message filter (`sentry:error_messages`, written via the
     project detail `options` blob as `filters:error_messages`), plus the five toggle filters
     (browser-extensions, legacy-browsers, web-crawlers, localhost, filtered-transaction), which
     do NOT live on the project object -- they have a dedicated endpoint
     (/projects/{org}/{slug}/filters/, one PUT per filter). We replicate whatever state the export
     carries.

This is a single, fully offline, export-driven tool. Org-level settings (governance, org-level
scrubbing defaults) are NOT part of this migration -- org options aren't reliably carried by the
relocation export, and were intentionally scoped out.

Export-only caveat: a relocation export carries `sentry.projectoption` rows for NON-DEFAULT
values only. So a filter/setting the customer never changed has no row and is left at the SaaS
default on the destination (we cannot normalise it without the self-hosted defaults table).

Project matching (greenfield assumption): source -> destination projects are paired by NAME
(case-insensitive); the destination's own slug is used for the PUT. A source project with no
name match on SaaS is skipped and reported (we never guess).

Usage:
  python migrate_project_settings.py <saas_token> <dest_org> --export-file <export.json> \
      [--source-org <slug>] [--saas-url https://sentry.io/api/0] [--dry-run]

The SaaS token needs `project:write`. No self-hosted token is required (offline / export-based).
"""
import json
import logging
import argparse
import requests
from datetime import datetime

import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "common"))
from export_source import ExportSource

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def _fmt(value, limit: int = 100) -> str:
    """Compact, readable rendering of a setting value (truncated if very long)."""
    s = json.dumps(value)
    return s if len(s) <= limit else s[: limit - 3] + "..."


# --- Whitelisted settings: SaaS project-detail field -> raw export option key ---
# All are flat fields on the project detail object, PUT in a single request and verified with a GET.
# Groups: (a) general settings, (b) custom grouping rules, (c) standard data scrubbers.
# groupingEnhancements + fingerprintingRules are custom, hand-authored grouping rules. They only
# affect how FUTURE events group (no re-grouping/forking of existing issues), SaaS validates their
# syntax on PUT, and they represent real customer tuning -- so they migrate. The grouping algorithm
# *version* (sentry:grouping_config) is deliberately excluded; see SKIPPED_OPTIONS.
# The data scrubbers (dataScrubber ... storeCrashReports) are the STANDARD privacy settings; advanced
# custom-PII (relay_pii_config / trusted relays) stays excluded -- see EXCLUDED_ADVANCED_OPTIONS.
SETTINGS_FIELD_TO_OPTION = {
    # (a) general settings
    "resolveAge": "sentry:resolve_age",
    "allowedDomains": "sentry:origins",
    "scrapeJavaScript": "sentry:scrape_javascript",
    "verifySSL": "sentry:verify_ssl",
    "subjectPrefix": "mail:subject_prefix",
    "subjectTemplate": "mail:subject_template",
    "defaultEnvironment": "sentry:default_environment",
    "highlightTags": "sentry:highlight_tags",
    "highlightContext": "sentry:highlight_context",
    # (b) custom grouping rules
    "groupingEnhancements": "sentry:grouping_enhancements",
    "fingerprintingRules": "sentry:fingerprinting_rules",
    # (c) standard data scrubbers (project level)
    "dataScrubber": "sentry:scrub_data",
    "dataScrubberDefaults": "sentry:scrub_defaults",
    "sensitiveFields": "sentry:sensitive_fields",
    "safeFields": "sentry:safe_fields",
    "scrubIPAddresses": "sentry:scrub_ip_address",
    "storeCrashReports": "sentry:store_crash_reports",
}

# The custom error-message filter: stored as a list under this option, written on the project
# detail under options["filters:error_messages"] as a newline-separated string.
ERROR_MESSAGES_OPTION = "sentry:error_messages"
ERROR_MESSAGES_FIELD = "filters:error_messages"

# The five toggle inbound filters, migrated via the dedicated /filters/ endpoint (NOT the project
# object). Stored in the export as `filters:<id>` = "1" | "0" | [subfilters] (legacy-browsers).
INBOUND_FILTER_IDS = [
    "browser-extensions",
    "legacy-browsers",
    "web-crawlers",
    "localhost",
    "filtered-transaction",
]

# Advanced custom-PII scrubbing deliberately NOT migrated (see DECISIONS.md D5); recorded, not dropped.
EXCLUDED_ADVANCED_OPTIONS = {
    "sentry:relay_pii_config",
    "sentry:trusted_relays",
}

# Deliberately NOT migrated: identity/secret, set-at-creation, or risky. grouping_config stays here
# on purpose (algorithm version, not custom rules -- see module docstring).
SKIPPED_OPTIONS = {
    "sentry:token",
    "sentry:token_header",
    "sentry:grouping_config",
    "sentry:secondary_grouping_config",
    "sentry:secondary_grouping_expiry",
    "sentry:builtin_symbol_sources",
    "sentry:dynamic_sampling_biases",
}


def build_settings_payload(options: dict) -> dict:
    """Map whitelisted raw option keys -> SaaS project-detail payload. Folds the custom
    error-message filter into an `options` sub-dict (only when non-empty, so we never blank it)."""
    payload = {}
    for field, key in SETTINGS_FIELD_TO_OPTION.items():
        if key in options:
            payload[field] = options[key]
    error_messages = options.get(ERROR_MESSAGES_OPTION)
    if error_messages:
        joined = "\n".join(error_messages) if isinstance(error_messages, list) else error_messages
        payload["options"] = {ERROR_MESSAGES_FIELD: joined}
    return payload


def build_filter_payloads(options: dict) -> dict:
    """Map `filters:<id>` option values -> {filter_id: put_body}. "1"/"0" -> {"active": bool};
    a legacy-browsers subfilter list -> {"subfilters": [...]} to preserve the exact selection."""
    payloads = {}
    for fid in INBOUND_FILTER_IDS:
        key = f"filters:{fid}"
        if key not in options:
            continue
        val = options[key]
        if fid == "legacy-browsers" and isinstance(val, (list, set, tuple)):
            payloads[fid] = {"subfilters": sorted(val)}
        elif isinstance(val, (list, set, tuple)):
            payloads[fid] = {"active": bool(val)}
        else:
            payloads[fid] = {"active": val in ("1", 1, True)}
    return payloads


class ProjectSettingsMigrator:
    """SaaS-side writer: PUTs settings/filters to the destination org and verifies via GET."""

    def __init__(self, auth_token: str, base_url: str = "https://sentry.io/api/0", dry_run: bool = False):
        self.base_url = base_url.rstrip("/")
        self.dry_run = dry_run
        self.headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json",
        }

    def list_projects(self, dest_org: str) -> list:
        """GET all destination projects (follows cursor pagination)."""
        results = []
        url = f"{self.base_url}/organizations/{dest_org}/projects/"
        params = None
        while url:
            resp = requests.get(url, headers=self.headers, params=params)
            resp.raise_for_status()
            results.extend(resp.json())
            url, params = self._next_link(resp.headers.get("Link")), None
        return results

    @staticmethod
    def _next_link(link_header: str):
        if not link_header:
            return None
        for part in link_header.split(","):
            if 'rel="next"' in part and 'results="true"' in part:
                start, end = part.find("<"), part.find(">")
                if start != -1 and end != -1:
                    return part[start + 1:end]
        return None

    def update_project(self, dest_org: str, dest_slug: str, payload: dict) -> dict:
        url = f"{self.base_url}/projects/{dest_org}/{dest_slug}/"
        if self.dry_run:
            logger.info(f"  action      : [DRY-RUN] would PUT {url} (not sent)")
            return {"dry_run": True}
        try:
            resp = requests.put(url, headers=self.headers, json=payload)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to update project '{dest_slug}': {e}")
            if hasattr(e, "response") and e.response is not None and hasattr(e.response, "text"):
                logger.error(f"Response: {e.response.text}")
            raise

    def get_project(self, dest_org: str, dest_slug: str) -> dict:
        url = f"{self.base_url}/projects/{dest_org}/{dest_slug}/"
        resp = requests.get(url, headers=self.headers)
        resp.raise_for_status()
        return resp.json()

    def verify(self, dest_org: str, dest_slug: str, payload: dict) -> dict:
        """Return {field: {expected, actual}} for any whitelisted field that didn't take. The
        `options` sub-dict (error-message filter) is compared key-by-key against the destination's
        own `options` blob and reported as `options.<key>`."""
        current = self.get_project(dest_org, dest_slug)
        mismatches = {}
        for k, expected in payload.items():
            if k == "options" and isinstance(expected, dict):
                cur_options = current.get("options") or {}
                for ok, oexp in expected.items():
                    if cur_options.get(ok) != oexp:
                        mismatches[f"options.{ok}"] = {"expected": oexp, "actual": cur_options.get(ok)}
                continue
            actual = current.get(k)
            if actual != expected:
                mismatches[k] = {"expected": expected, "actual": actual}
        return mismatches

    def get_project_filters(self, dest_org: str, dest_slug: str) -> list:
        url = f"{self.base_url}/projects/{dest_org}/{dest_slug}/filters/"
        resp = requests.get(url, headers=self.headers)
        resp.raise_for_status()
        return resp.json()

    def update_filter(self, dest_org: str, dest_slug: str, filter_id: str, body: dict) -> dict:
        """PUT a single inbound filter. The endpoint returns 204 No Content on success."""
        url = f"{self.base_url}/projects/{dest_org}/{dest_slug}/filters/{filter_id}/"
        if self.dry_run:
            logger.info(f"  action      : [DRY-RUN] would PUT {url} {_fmt(body)} (not sent)")
            return {"dry_run": True}
        try:
            resp = requests.put(url, headers=self.headers, json=body)
            resp.raise_for_status()
            return {"status_code": resp.status_code}
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to update filter '{filter_id}' on '{dest_slug}': {e}")
            if hasattr(e, "response") and e.response is not None and hasattr(e.response, "text"):
                logger.error(f"Response: {e.response.text}")
            raise

    def verify_filters(self, dest_org: str, dest_slug: str, desired: dict) -> dict:
        """Compare each desired filter state against the destination's live /filters/ state.
        Booleans compare directly; legacy-browsers subfilter lists compare as sets."""
        current = {f["id"]: f.get("active") for f in self.get_project_filters(dest_org, dest_slug)
                   if isinstance(f, dict)}
        mismatches = {}
        for fid, body in desired.items():
            actual = current.get(fid)
            if "subfilters" in body:
                expected = set(body["subfilters"])
                actual_set = set(actual) if isinstance(actual, (list, set, tuple)) else actual
                if actual_set != expected:
                    mismatches[fid] = {"expected": sorted(expected), "actual": actual}
            else:
                if bool(actual) != bool(body.get("active")):
                    mismatches[fid] = {"expected": body.get("active"), "actual": actual}
        return mismatches


def _classify_present(options: dict, migrated_keys: set) -> dict:
    """Full accounting of every option present on the source project (no silent drops)."""
    excluded = sorted(k for k in options if k in EXCLUDED_ADVANCED_OPTIONS)
    skipped = sorted(k for k in options if k in SKIPPED_OPTIONS)
    handled = migrated_keys | EXCLUDED_ADVANCED_OPTIONS | SKIPPED_OPTIONS
    unhandled = sorted(k for k in options if k not in handled)
    return {"excluded_advanced": excluded, "skipped": skipped, "unhandled": unhandled}


def main():
    parser = argparse.ArgumentParser(description="Migrate per-project settings from a relocation export -> SaaS")
    parser.add_argument("auth_token", help="SaaS auth token (needs project:write)")
    parser.add_argument("dest_org", help="Destination SaaS org slug")
    parser.add_argument("--export-file", required=True, help="Path to the relocation export JSON")
    parser.add_argument("--source-org", default=None,
                        help="Optional: restrict to this org slug (for multi-org export files)")
    parser.add_argument("--saas-url", default="https://sentry.io/api/0", help="SaaS API base URL")
    parser.add_argument("--run_on_real_data", type=lambda v: str(v).strip().lower() in ('true', '1', 'yes', 'y'),
                        default=False, metavar="true|false",
                        help="Set to true to actually perform changes. Default false = dry-run.")
    parser.add_argument("--dry-run", action="store_true", dest="_dry_run_noop",
                        help="(default) Dry-run is on by default; accepted for compatibility and is a no-op.")
    args = parser.parse_args()
    args.dry_run = not args.run_on_real_data

    if args.dry_run:
        logger.info("=== DRY RUN (default): no changes will be made to SaaS. Pass --run_on_real_data=true to apply. ===")

    source = ExportSource(args.export_file)
    migrator = ProjectSettingsMigrator(args.auth_token, base_url=args.saas_url, dry_run=args.dry_run)

    logger.info(f"Parsing source projects from export '{args.export_file}' ...")
    src_projects = source.get_projects(args.source_org)
    logger.info(f"Found {len(src_projects)} source project(s)"
                + (f" in org '{args.source_org}'." if args.source_org else " across all orgs in the export."))
    if not src_projects and not args.source_org:
        logger.warning(f"No projects found. Orgs present in export: {source.org_slugs()}")

    logger.info(f"Listing destination projects from SaaS org '{args.dest_org}' ...")
    dest_projects = migrator.list_projects(args.dest_org)
    dest_by_name = {p["name"].strip().lower(): p for p in dest_projects}
    logger.info(f"Found {len(dest_projects)} destination project(s).")

    migrated_option_keys = set(SETTINGS_FIELD_TO_OPTION.values()) | {ERROR_MESSAGES_OPTION} \
        | {f"filters:{fid}" for fid in INBOUND_FILTER_IDS}

    per_project = []
    unmatched = []

    for src in src_projects:
        name = src.get("name", "") or ""
        dest = dest_by_name.get(name.strip().lower())
        if not dest:
            logger.warning(f"No SaaS project named '{name}' (source slug '{src.get('slug')}') -- skipping.")
            unmatched.append({"source_name": name, "source_slug": src.get("slug")})
            continue

        dest_slug = dest["slug"]
        options = source.options_for(src["pk"])
        payload = build_settings_payload(options)
        filter_payloads = build_filter_payloads(options)
        accounting = _classify_present(options, migrated_option_keys)

        logger.info("")
        logger.info("-" * 64)
        logger.info(f"Project: {name}")
        logger.info(f"  source slug : {src['slug']}")
        logger.info(f"  dest slug   : {dest_slug}")
        if payload:
            key_w = max(len(k) for k in payload)
            logger.info(f"  settings applied ({len(payload)}):")
            for k, v in payload.items():
                logger.info(f"      {k.ljust(key_w)} = {_fmt(v)}")
        else:
            logger.info("  settings applied (0): none present on source")
        if accounting["excluded_advanced"]:
            logger.info(f"  excluded    : {len(accounting['excluded_advanced'])} advanced custom-PII "
                        f"option(s) not migrated (see DECISIONS.md D5): {', '.join(accounting['excluded_advanced'])}")

        migrator.update_project(args.dest_org, dest_slug, payload)

        mismatches = {}
        if args.dry_run:
            logger.info("  verify      : skipped (dry-run)")
        else:
            mismatches = migrator.verify(args.dest_org, dest_slug, payload)
            if mismatches:
                logger.warning(f"  verify      : MISMATCH {_fmt(mismatches, 200)}")
            else:
                logger.info("  verify      : passed")

        # --- Inbound filters (dedicated /filters/ endpoint; one PUT per filter) ---
        filter_mismatches = {}
        if filter_payloads:
            logger.info(f"  filters applied ({len(filter_payloads)}):")
            fid_w = max(len(f) for f in filter_payloads)
            for fid, body in filter_payloads.items():
                logger.info(f"      {fid.ljust(fid_w)} = {_fmt(body)}")
                migrator.update_filter(args.dest_org, dest_slug, fid, body)
            if args.dry_run:
                logger.info("  filters verify: skipped (dry-run)")
            else:
                filter_mismatches = migrator.verify_filters(args.dest_org, dest_slug, filter_payloads)
                if filter_mismatches:
                    logger.warning(f"  filters verify: MISMATCH {_fmt(filter_mismatches, 200)}")
                else:
                    logger.info("  filters verify: passed")
        else:
            logger.info("  filters applied (0): no filter options present on source")

        per_project.append({
            "source_name": name,
            "source_slug": src["slug"],
            "dest_slug": dest_slug,
            "applied": payload,
            "filters_applied": filter_payloads,
            "excluded_advanced": accounting["excluded_advanced"],
            "skipped": accounting["skipped"],
            "unhandled": accounting["unhandled"],
            "verification_mismatches": mismatches,
            "filter_verification_mismatches": filter_mismatches,
        })

    results = {
        "timestamp": datetime.now().isoformat(),
        "export_file": args.export_file,
        "source_org": args.source_org,
        "dest_org": args.dest_org,
        "dry_run": args.dry_run,
        "matched_count": len(per_project),
        "unmatched_count": len(unmatched),
        "projects": per_project,
        "unmatched": unmatched,
    }
    with open("project_settings_migration_results.json", "w") as f:
        json.dump(results, f, indent=2)

    logger.info("")
    logger.info("=" * 64)
    mode = "DRY RUN (nothing written)" if args.dry_run else "LIVE"
    logger.info(f"Summary [{mode}]: matched {len(per_project)}, unmatched {len(unmatched)}")
    if per_project:
        name_w = max(len(p["source_name"]) for p in per_project)
        for p in per_project:
            if args.dry_run:
                status = "would apply"
            else:
                clean = not p["verification_mismatches"] and not p.get("filter_verification_mismatches")
                status = "OK" if clean else "MISMATCH"
            logger.info(
                f"  {p['source_name'].ljust(name_w)}  {p['source_slug']} -> {p['dest_slug']}"
                f"  ({len(p['applied'])} settings, {len(p.get('filters_applied', {}))} filters)  {status}"
            )
    for u in unmatched:
        logger.info(f"  [UNMATCHED] {u['source_name']} (source slug '{u['source_slug']}') - no SaaS project by that name")
    logger.info("")
    logger.info("Wrote project_settings_migration_results.json")


import os as _rl_os, sys as _rl_sys
_rl_sys.path.insert(0, _rl_os.path.join(_rl_os.path.dirname(_rl_os.path.abspath(__file__)), "..", "common"))
_rl_sys.path.insert(0, _rl_os.path.join(_rl_os.path.dirname(_rl_os.path.abspath(__file__)), "common"))
from run_logging import start_run_log


if __name__ == "__main__":
    start_run_log("migrate_project_settings")
    main()
