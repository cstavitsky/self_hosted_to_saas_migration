#!/usr/bin/env python3
"""
One-off helper (NOT part of the core toolkit) — TEST ENVIRONMENTS ONLY.

Problem: a freshly migrated SaaS project has no events, so it has no environments.
Alert rules scoped to an environment (e.g. "production") then fail on creation with
"This environment has not been created." Environments in Sentry only come into being
when an event tagged with them is ingested.

This script reads the export, finds every (project, environment) pair the alerts
reference, looks up each project's DSN in the destination org, and sends ONE benign
seed event per pair so the environment gets registered. Run it BEFORE migrating
environment-scoped alerts.

Dry by default (lists the pairs). Pass --run_on_real_data=true to fetch DSNs and send.

*** Use this on the throwaway TEST org only. For the real cutover, do NOT fabricate
events in the customer's projects — instead confirm real data is already flowing (which
creates the environments naturally), then migrate alerts. ***

Usage:
  python3 seed_environments.py "$SAAS_TOKEN" "$DEST_ORG" "$EXPORT" --source-org my-org
  python3 seed_environments.py "$SAAS_TOKEN" "$DEST_ORG" "$EXPORT" --source-org my-org --run_on_real_data=true
"""
import argparse
import json
import logging
import sys
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)
BASE = "https://sentry.io/api/0"


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


def collect_pairs(data, source_pk):
    """Distinct (project_slug, environment_name) pairs referenced by issue-alert rules."""
    proj = {o["pk"]: o["fields"].get("slug") for o in data
            if isinstance(o, dict) and o.get("model") == "sentry.project"
            and (source_pk is None or o["fields"].get("organization") == source_pk)}
    envs = {o["pk"]: o["fields"].get("name") for o in data
            if isinstance(o, dict) and o.get("model") == "sentry.environment"}
    pairs = set()
    for o in data:
        if not isinstance(o, dict) or o.get("model") != "sentry.rule":
            continue
        f = o.get("fields", {})
        if f.get("project") not in proj:
            continue
        env_id = f.get("environment_id")
        if env_id and envs.get(env_id):
            pairs.add((proj[f["project"]], envs[env_id]))
    return sorted(pairs)


def get_project_dsn(token, org, slug):
    r = requests.get(f"{BASE}/projects/{org}/{slug}/keys/",
                     headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    keys = r.json()
    if not keys:
        return None
    return keys[0]["dsn"]["public"]


def send_seed_event(dsn, environment):
    u = urlparse(dsn)
    store_url = f"{u.scheme}://{u.hostname}/api/{u.path.strip('/')}/store/"
    headers = {
        "X-Sentry-Auth": (f"Sentry sentry_version=7, sentry_key={u.username}, "
                          f"sentry_client=migration-env-seed/1.0"),
        "Content-Type": "application/json",
    }
    body = {
        "event_id": uuid.uuid4().hex,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "platform": "other",
        "level": "info",
        "message": f"migration environment seed ({environment}) — safe to resolve",
        "environment": environment,
    }
    r = requests.post(store_url, json=body, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json().get("id")


def main():
    p = argparse.ArgumentParser(description="Seed environments for a TEST org (dry by default).")
    p.add_argument("token")
    p.add_argument("org")
    p.add_argument("export_file")
    p.add_argument("--source-org")
    p.add_argument("--run_on_real_data",
                   type=lambda v: str(v).strip().lower() in ("true", "1", "yes", "y"),
                   default=False, help="Set true to actually fetch DSNs and send events.")
    args = p.parse_args()

    data = json.load(open(args.export_file))
    source_pk = resolve_source_pk(data, args.source_org)
    pairs = collect_pairs(data, source_pk)
    logger.info(f"=== {'LIVE (sending seed events)' if args.run_on_real_data else 'DRY RUN (nothing sent)'} ===")
    logger.info(f"{len(pairs)} (project, environment) pair(s) referenced by alerts")

    by_project = {}
    sent = failed = 0
    for slug, env in pairs:
        if not args.run_on_real_data:
            logger.info(f"  [DRY-RUN] would seed {slug} / environment '{env}'")
            continue
        try:
            dsn = by_project.get(slug) or get_project_dsn(args.token, args.org, slug)
            by_project[slug] = dsn
            if not dsn:
                logger.error(f"  {slug}: no DSN found; skipping")
                failed += 1
                continue
            eid = send_seed_event(dsn, env)
            logger.info(f"  seeded {slug} / '{env}' (event {eid})")
            sent += 1
        except requests.exceptions.RequestException as e:
            logger.error(f"  {slug} / '{env}': {e}")
            failed += 1

    if args.run_on_real_data:
        logger.info(f"Done. seeded: {sent}, failed: {failed}. "
                    f"Environments may take a few seconds to register before alert creation.")
    else:
        logger.info("Dry run complete. Re-run with --run_on_real_data=true to send seed events.")


if __name__ == "__main__":
    main()
