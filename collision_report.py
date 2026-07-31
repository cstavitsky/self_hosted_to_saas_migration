#!/usr/bin/env python3
"""
Merge-collision pre-flight (read-only) — for migrating a SECOND instance (e.g. V24) into a SaaS org
that ALREADY contains a first instance (e.g. V25), without overwriting the first one.

Compares the export you're about to migrate against what LIVE-EXISTS in the destination org right
now, and flags name collisions per the agreed merge policy:

  * PROJECTS  — first instance wins. A colliding project slug will be SKIPPED (its settings and
    alerts must be skipped too, or they'd overwrite the existing project). Reported here.
  * TEAMS     — no overwrite; the second instance's members are MERGED into the existing team
    (the assign-members step just adds members). Reported here for awareness (not a problem).

Read-only: never writes to SaaS. Writes a skip-list file `collision_skip_projects_<tag>.json`
(the colliding project slugs) that the run steps consume via --skip-existing-projects, so the
skip is based on the exact set reported here.

Usage:
  python3 collision_report.py "$SAAS_TOKEN" "$DEST_ORG" "$EXPORT" --source-org "$SRC_ORG"
"""
import argparse
import json
import logging
import sys
from datetime import datetime

import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)
BASE = "https://sentry.io/api/0"


def paginate(session, url):
    while url:
        r = session.get(url)
        r.raise_for_status()
        for item in r.json():
            yield item
        nxt = r.links.get("next", {})
        url = nxt.get("url") if nxt.get("results") == "true" else None


def resolve_source_pk(data, source_org):
    orgs = {o.get("pk"): (o.get("fields") or {}).get("slug")
            for o in data if isinstance(o, dict) and o.get("model") == "sentry.organization"}
    if source_org:
        hits = [pk for pk, s in orgs.items() if s == source_org]
        if not hits:
            sys.exit(f"--source-org '{source_org}' not found. Orgs: {sorted(filter(None, orgs.values()))}")
        return hits[0]
    if len(orgs) > 1:
        sys.exit(f"Export has {len(orgs)} orgs; pass --source-org.")
    return next(iter(orgs), None)


def export_slugs(data, model, source_pk):
    return {o["fields"].get("slug") for o in data
            if isinstance(o, dict) and o.get("model") == model
            and (source_pk is None or o["fields"].get("organization") == source_pk)
            and o["fields"].get("slug")}


def main():
    p = argparse.ArgumentParser(description="Report merge collisions vs the live destination org (read-only).")
    p.add_argument("auth_token")
    p.add_argument("org_slug", help="Destination org that already holds the first instance")
    p.add_argument("export_file", help="Export of the SECOND instance you're about to migrate")
    p.add_argument("--source-org", help="Source org slug within the export")
    args = p.parse_args()

    data = json.load(open(args.export_file))
    source_pk = resolve_source_pk(data, args.source_org)
    exp_projects = export_slugs(data, "sentry.project", source_pk)
    exp_teams = export_slugs(data, "sentry.team", source_pk)

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {args.auth_token}"})
    logger.info(f"Reading live org '{args.org_slug}' ...")
    live_projects = {p.get("slug") for p in paginate(session, f"{BASE}/organizations/{args.org_slug}/projects/")}
    live_teams = {t.get("slug") for t in paginate(session, f"{BASE}/organizations/{args.org_slug}/teams/")}

    proj_collisions = sorted(exp_projects & live_projects)
    team_collisions = sorted(exp_teams & live_teams)

    logger.info("=" * 70)
    logger.info(f"PROJECTS in export: {len(exp_projects)} | already live: {len(live_projects)} | "
                f"COLLISIONS: {len(proj_collisions)}")
    logger.info("  -> these project slugs already exist and will be SKIPPED (first instance wins;")
    logger.info("     their settings + alerts are skipped too so nothing is overwritten):")
    for s in proj_collisions:
        logger.info(f"       - {s}")
    logger.info("-" * 70)
    logger.info(f"TEAMS in export: {len(exp_teams)} | already live: {len(live_teams)} | "
                f"COLLISIONS: {len(team_collisions)}")
    logger.info("  -> these team slugs already exist; the export's members will be MERGED into the")
    logger.info("     existing team (no overwrite). This is expected, not a problem:")
    for s in team_collisions:
        logger.info(f"       - {s}")
    logger.info("=" * 70)

    tag = f"{args.source_org or 'allorgs'}_{args.org_slug}_{datetime.now():%Y%m%d_%H%M%S}"
    out = {
        "destination_org": args.org_slug,
        "source_org": args.source_org,
        "projects_skip": proj_collisions,          # feed to --skip-existing-projects
        "projects_new": sorted(exp_projects - live_projects),
        "teams_merge": team_collisions,
        "teams_new": sorted(exp_teams - live_teams),
    }
    fname = f"collision_skip_projects_{tag}.json"
    with open(fname, "w") as f:
        json.dump(out, f, indent=2)
    logger.info(f"Wrote {fname}")
    logger.info(f"Next: pass  --skip-existing-projects {fname}  to create_sentry_projects.py, "
                f"migrate_project_settings.py, and migrate_alert_rules.py on the second-instance run.")


if __name__ == "__main__":
    try:
        main()
    except requests.exceptions.RequestException as e:
        logger.error(f"API error: {e}")
        sys.exit(1)
