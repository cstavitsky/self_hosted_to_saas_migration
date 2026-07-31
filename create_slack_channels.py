#!/usr/bin/env python3
"""
One-off helper (NOT part of the migration toolkit).

Creates Slack channels in a DUMMY workspace so migrated alert rules that notify
those channels have somewhere to land during real-time testing.

Dry by default (lists what it WOULD create). Pass --run_on_real_data=true to create.

Requires a Slack BOT token (xoxb-...) for an app installed in YOUR throwaway
workspace, with scopes: channels:manage (public) and/or groups:write (private).
This is a Slack token - NOT your Sentry SAAS_TOKEN.

Usage:
  export SLACK_BOT_TOKEN=xoxb-...
  python3 create_slack_channels.py "$SLACK_BOT_TOKEN"                         # dry run, reads slack_test_channels.txt
  python3 create_slack_channels.py "$SLACK_BOT_TOKEN" --run_on_real_data=true
  python3 create_slack_channels.py "$SLACK_BOT_TOKEN" --only ct-framecore-alerts-sentry --run_on_real_data=true
  python3 create_slack_channels.py "$SLACK_BOT_TOKEN" --private --file slack_test_channels.txt
"""
import argparse
import logging
import re
import sys

import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

API = "https://slack.com/api/conversations.create"
API_LIST = "https://slack.com/api/conversations.list"
API_INVITE = "https://slack.com/api/conversations.invite"


def normalize(name):
    """Slack channel names: lowercase, <=80 chars, only letters/digits/hyphen/underscore."""
    n = name.strip().lstrip("#").lower()
    n = re.sub(r"[^a-z0-9_-]+", "-", n).strip("-")
    return n[:80]


def main():
    p = argparse.ArgumentParser(description="Create Slack channels for alert testing (dry by default).")
    p.add_argument("bot_token", help="Slack bot token (xoxb-...) - NOT your Sentry token")
    p.add_argument("--file", default="slack_test_channels.txt", help="File with one channel name per line")
    p.add_argument("--only", action="append", metavar="NAME",
                   help="Only create this channel (repeatable); overrides --file selection")
    p.add_argument("--private", action="store_true", help="Create private channels instead of public")
    p.add_argument("--invite", metavar="USER_ID",
                   help="Instead of creating, invite this Slack member ID to every channel in the "
                        "list that already exists (get your ID from Slack: profile > ... > Copy member ID).")
    p.add_argument("--run_on_real_data",
                   type=lambda v: str(v).strip().lower() in ("true", "1", "yes", "y"),
                   default=False, help="Set true to actually create. Default false = dry-run.")
    args = p.parse_args()

    if args.only:
        names = args.only
    else:
        try:
            with open(args.file) as f:
                names = [ln for ln in (l.strip() for l in f) if ln and not ln.startswith("#")]
        except FileNotFoundError:
            logger.error(f"Channel list file not found: {args.file}")
            sys.exit(1)

    names = [normalize(n) for n in names]
    names = [n for n in names if n]
    live = args.run_on_real_data
    logger.info(f"=== {'LIVE (creating)' if live else 'DRY RUN (nothing created)'} === "
                f"{len(names)} channel(s), {'private' if args.private else 'public'}")

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {args.bot_token}"})

    # --- Invite mode: add a user to every listed channel that exists ---
    if args.invite:
        # build name -> id map from the workspace's channel list (handles pagination)
        name_to_id, cursor = {}, None
        while True:
            resp = session.get(API_LIST, params={"limit": 1000, "exclude_archived": "true",
                                                  "types": "public_channel,private_channel",
                                                  **({"cursor": cursor} if cursor else {})})
            d = resp.json()
            if not d.get("ok"):
                logger.error(f"conversations.list failed: {d.get('error')}"); sys.exit(1)
            for ch in d.get("channels", []):
                name_to_id[ch["name"]] = ch["id"]
            cursor = d.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        invited = missing = failed = 0
        for name in names:
            cid = name_to_id.get(name)
            if not cid:
                logger.warning(f"  #{name}: not found in workspace (skipping)"); missing += 1; continue
            if not live:
                logger.info(f"  [DRY-RUN] would invite {args.invite} to #{name}"); continue
            r = session.post(API_INVITE, json={"channel": cid, "users": args.invite}).json()
            if r.get("ok") or r.get("error") == "already_in_channel":
                logger.info(f"  invited to #{name}"); invited += 1
            else:
                logger.error(f"  FAILED #{name}: {r.get('error')}"); failed += 1
        logger.info(f"Done (invite). invited: {invited}, not found: {missing}, failed: {failed}"
                    if live else "Dry run complete. Re-run with --run_on_real_data=true to invite.")
        return

    created = existing = failed = 0

    for name in names:
        if not live:
            logger.info(f"  [DRY-RUN] would create #{name}")
            continue
        resp = session.post(API, json={"name": name, "is_private": args.private})
        data = resp.json()
        if data.get("ok"):
            cid = data["channel"]["id"]
            logger.info(f"  created #{name} ({cid})")
            created += 1
        elif data.get("error") == "name_taken":
            logger.info(f"  exists  #{name} (skipping)")
            existing += 1
        else:
            logger.error(f"  FAILED  #{name}: {data.get('error')}")
            failed += 1

    if live:
        logger.info(f"Done. created: {created}, already existed: {existing}, failed: {failed}")
    else:
        logger.info("Dry run complete. Re-run with --run_on_real_data=true to create.")


if __name__ == "__main__":
    try:
        main()
    except requests.exceptions.RequestException as e:
        logger.error(f"Slack API error: {e}")
        sys.exit(1)
