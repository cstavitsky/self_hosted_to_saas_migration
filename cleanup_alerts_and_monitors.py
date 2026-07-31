#!/usr/bin/env python3
"""
One-off teardown helper (NOT part of the core toolkit).

Clears ALL alerting/monitoring objects from a destination SaaS org so a test org can be reset to a
clean slate before a fresh migration run. Deletes:

  1. Metric alerts  (legacy alert-rule objects)   - /organizations/{org}/alert-rules/
  2. Issue alerts   (per project)                 - /projects/{org}/{project}/rules/
  3. Cron monitors                                - /organizations/{org}/monitors/
  4. Detectors      (the new "Monitors" view; metric monitors are type 'metric_issue')
                                                  - /organizations/{org}/detectors/

Note: in the new "Monitors & Alerts" model a metric alert is backed by BOTH a legacy alert-rule
and a workflow-engine "detector". Deleting the alert-rule alone leaves the detector (the thing you
see under Monitors), so this script deletes detectors too.

IMPORTANT — "Error Monitor" detectors are NOT deletable (and that's expected): Sentry auto-creates
one default per-project error detector named "Error Monitor" (type 'error') for EVERY project. These
are Sentry-managed system defaults ("Monitors managed by Sentry cannot be deleted" in the UI) and
cannot be removed via UI or API — they only go away if the project itself is deleted. So a freshly
migrated org will always show one "Error Monitor" per project (e.g. 270 projects -> 270 Error
Monitors); that is normal, not leftover clutter. This script therefore targets ONLY 'metric_issue'
detectors (the migrated metric monitors) by default and leaves the Error Monitors alone.

Dry by default (lists what it WOULD delete). Pass --run_on_real_data=true to actually delete.
Scope is the single org you pass in; it never touches any other org.

*** Intended for the throwaway TEST org. Do not point this at a real customer org. ***

Usage:
  python3 cleanup_alerts_and_monitors.py "$SAAS_TOKEN" "$DEST_ORG"                       # dry run (everything)
  python3 cleanup_alerts_and_monitors.py "$SAAS_TOKEN" "$DEST_ORG" --run_on_real_data=true
  python3 cleanup_alerts_and_monitors.py "$SAAS_TOKEN" "$DEST_ORG" --metric-only ...      # restrict scope
  python3 cleanup_alerts_and_monitors.py "$SAAS_TOKEN" "$DEST_ORG" --issue-only ...
  python3 cleanup_alerts_and_monitors.py "$SAAS_TOKEN" "$DEST_ORG" --crons-only ...

Token scopes: alerts:write (metric alerts), project:write/admin (issue alerts + cron monitors).
"""
import argparse
import collections
import logging
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)
BASE = "https://sentry.io/api/0"


def paginate(session, url):
    """Yield items across Sentry's cursor-paginated list endpoints."""
    while url:
        r = session.get(url)
        r.raise_for_status()
        for item in r.json():
            yield item
        nxt = r.links.get("next", {})
        url = nxt.get("url") if nxt.get("results") == "true" else None


def delete_each(session, live, items, url_for, label_for, kind):
    deleted = failed = 0
    for it in items:
        label = label_for(it)
        url = url_for(it)
        if not live:
            logger.info(f"  [DRY-RUN] DELETE {kind}: {label}")
            continue
        resp = session.delete(url)
        if resp.status_code in (200, 202, 204):
            logger.info(f"  deleted {kind}: {label}")
            deleted += 1
        else:
            logger.error(f"  FAILED {kind}: {label} -> {resp.status_code} {resp.text[:120]}")
            failed += 1
    return deleted, failed


def main():
    p = argparse.ArgumentParser(description="Delete alerts + monitors from a SaaS org (dry by default).")
    p.add_argument("auth_token", help="SaaS token (alerts:write + project:write/admin)")
    p.add_argument("org_slug", help="Destination SaaS org slug (the ONLY org touched)")
    p.add_argument("--run_on_real_data",
                   type=lambda v: str(v).strip().lower() in ("true", "1", "yes", "y"),
                   default=False, help="Set true to actually delete. Default false = dry-run.")
    p.add_argument("--metric-only", action="store_true", help="Only delete metric alerts (legacy alert-rules)")
    p.add_argument("--issue-only", action="store_true", help="Only delete issue alerts")
    p.add_argument("--crons-only", action="store_true", help="Only delete cron monitors")
    p.add_argument("--detectors-only", action="store_true",
                   help="Only delete detectors (the new 'Monitors', e.g. metric monitors)")
    p.add_argument("--all-detector-types", action="store_true",
                   help="Delete detectors of ALL types (default only deletes type 'metric_issue', the "
                        "metric monitors, to avoid removing default per-project error detectors)")
    args = p.parse_args()

    # If no scope flag is set, do everything. If any is set, do only the selected ones.
    scoped = args.metric_only or args.issue_only or args.crons_only or args.detectors_only
    do_metric = args.metric_only or not scoped
    do_issue = args.issue_only or not scoped
    do_crons = args.crons_only or not scoped
    do_detectors = args.detectors_only or not scoped

    live = args.run_on_real_data
    org = args.org_slug
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {args.auth_token}"})
    logger.info(f"=== {'LIVE (deleting)' if live else 'DRY RUN (nothing deleted)'} === org: {org}")

    totals = {}

    # 1. Metric alerts (metric monitors)
    if do_metric:
        rules = list(paginate(session, f"{BASE}/organizations/{org}/alert-rules/"))
        logger.info(f"Metric alerts found: {len(rules)}")
        totals["metric"] = delete_each(
            session, live, rules,
            url_for=lambda r: f"{BASE}/organizations/{org}/alert-rules/{r.get('id')}/",
            label_for=lambda r: f"{r.get('id')} '{r.get('name')}'", kind="metric alert")

    # 2. Issue alerts (per project)
    if do_issue:
        projects = list(paginate(session, f"{BASE}/organizations/{org}/projects/"))
        logger.info(f"Projects to scan for issue alerts: {len(projects)}")
        d = f = 0
        for proj in projects:
            slug = proj.get("slug")
            rules = list(paginate(session, f"{BASE}/projects/{org}/{slug}/rules/"))
            dd, ff = delete_each(
                session, live, rules,
                url_for=lambda r, s=slug: f"{BASE}/projects/{org}/{s}/rules/{r.get('id')}/",
                label_for=lambda r, s=slug: f"{r.get('id')} '{r.get('name')}' (project {s})",
                kind="issue alert")
            d += dd; f += ff
        totals["issue"] = (d, f)

    # 3. Cron monitors
    if do_crons:
        monitors = list(paginate(session, f"{BASE}/organizations/{org}/monitors/"))
        logger.info(f"Cron monitors found: {len(monitors)}")
        totals["crons"] = delete_each(
            session, live, monitors,
            url_for=lambda m: f"{BASE}/organizations/{org}/monitors/{m.get('slug') or m.get('id')}/",
            label_for=lambda m: f"{m.get('slug') or m.get('id')} '{m.get('name')}'", kind="cron monitor")

    # 4. Detectors (the new "Monitors" — metric alerts show here as type 'metric_issue')
    if do_detectors:
        detectors = list(paginate(session, f"{BASE}/organizations/{org}/detectors/"))
        by_type = collections.Counter(d.get("type") for d in detectors)
        logger.info(f"Detectors found: {len(detectors)} (by type: {dict(by_type)})")
        if args.all_detector_types:
            target = detectors
        else:
            target = [d for d in detectors if d.get("type") == "metric_issue"]
            logger.info(f"  targeting type 'metric_issue' only: {len(target)} "
                        f"(use --all-detector-types to delete every type)")
        totals["detectors"] = delete_each(
            session, live, target,
            url_for=lambda d: f"{BASE}/organizations/{org}/detectors/{d.get('id')}/",
            label_for=lambda d: f"{d.get('id')} '{d.get('name')}' (type {d.get('type')})", kind="detector")

    if live:
        summary = ", ".join(f"{k}: {v[0]} deleted/{v[1]} failed" for k, v in totals.items())
        logger.info(f"Done. {summary}")
    else:
        logger.info("Dry run complete. Re-run with --run_on_real_data=true to delete.")


if __name__ == "__main__":
    try:
        main()
    except requests.exceptions.RequestException as e:
        logger.error(f"API error: {e}")
        sys.exit(1)
