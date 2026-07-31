#!/usr/bin/env python3
"""EXPERIMENT: carry a self-hosted Slack alert action over to SaaS.

This is a spike, not part of the supported toolkit. It proves whether an issue
alert's Slack notification action can survive migration *if* the same Slack
workspace is already installed on the destination SaaS org.

What it does differently from core/migrate_alert_rules.py:
  - it does NOT replace the Slack action with email
  - it looks up the SaaS Slack integration id and REWRITES the action's
    `workspace` field to that id (keeping `channel` / `channel_id`)
  - it POSTs the rule and, because SaaS validates Slack channels asynchronously,
    polls the rule-task endpoint until the rule is actually created or fails

Usage:
  export SAAS_TOKEN=...            # needs org:read (integrations) + alerts:write/project:write
  export DEST_ORG=...
  python3 experiments/slack_action_carryover.py "$SAAS_TOKEN" "$DEST_ORG" \
      export.json --only "migration-slack-test" [--workspace <saas_integration_id>] [--dry-run]
"""
import argparse
import json
import sys
import time

import requests

BASE = "https://sentry.io/api/0"
SLACK_ACTION_ID = "sentry.integrations.slack.notify_action.SlackNotifyServiceAction"


def load_export(path):
    with open(path) as f:
        return json.load(f)


def project_slug_by_pk(data):
    return {o["pk"]: o["fields"]["slug"] for o in data
            if isinstance(o, dict) and o.get("model") == "sentry.project"}


def find_rule(data, label):
    for o in data:
        if isinstance(o, dict) and o.get("model") == "sentry.rule" and o["fields"].get("label") == label:
            return o
    return None


def parse_blob(fields):
    raw = fields.get("data")
    return json.loads(raw) if isinstance(raw, str) else (raw or {})


def get_slack_integrations(token, org):
    r = requests.get(
        f"{BASE}/organizations/{org}/integrations/",
        params={"provider_key": "slack"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def create_rule(token, org, slug, payload, dry_run):
    url = f"{BASE}/projects/{org}/{slug}/rules/"
    if dry_run:
        print(f"[DRY-RUN] POST {url}\n{json.dumps(payload, indent=2)}")
        return {"dry_run": True}
    r = requests.post(url, json=payload,
                      headers={"Authorization": f"Bearer {token}"}, timeout=60)
    # Slack rules validate the channel async: SaaS may return a task uuid to poll.
    if r.status_code in (200, 201):
        return r.json()
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code == 202 and "uuid" in body:
        return poll_task(token, org, slug, body["uuid"])
    print(f"  ERROR {r.status_code}: {r.text[:500]}")
    r.raise_for_status()
    return body


def poll_task(token, org, slug, uuid, tries=20, delay=1.5):
    url = f"{BASE}/projects/{org}/{slug}/rule-task/{uuid}/"
    for _ in range(tries):
        time.sleep(delay)
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        r.raise_for_status()
        data = r.json()
        status = data.get("status")
        if status == "success":
            return data.get("rule", data)
        if status == "failed":
            raise RuntimeError(f"Slack channel validation failed: {data.get('error')}")
    raise TimeoutError("rule-task polling timed out (channel never validated)")


def main():
    ap = argparse.ArgumentParser(description="Experiment: carry Slack alert action to SaaS")
    ap.add_argument("token")
    ap.add_argument("org")
    ap.add_argument("export_file")
    ap.add_argument("--only", required=True, help="alert label to migrate")
    ap.add_argument("--workspace", help="SaaS Slack integration id (auto-detected if one exists)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rebind-channel", action="store_true",
                    help="Drop the source channel_id so SaaS resolves the Slack action by channel "
                         "NAME instead. Use when testing against a DIFFERENT workspace (e.g. a dummy), "
                         "where the original channel_id does not exist.")
    args = ap.parse_args()

    data = load_export(args.export_file)
    rule = find_rule(data, args.only)
    if not rule:
        sys.exit(f"No sentry.rule found with label {args.only!r}")

    fields = rule["fields"]
    blob = parse_blob(fields)
    slug = project_slug_by_pk(data).get(fields.get("project"))
    if not slug:
        sys.exit(f"No project slug for project pk {fields.get('project')}")

    slack_action = next((a for a in blob.get("actions", []) if a.get("id") == SLACK_ACTION_ID), None)
    if not slack_action:
        sys.exit(f"Alert {args.only!r} has no Slack action to carry over")

    print(f"Source Slack action: workspace={slack_action.get('workspace')} "
          f"channel={slack_action.get('channel')} channel_id={slack_action.get('channel_id')}")

    # Resolve destination Slack integration id ("workspace" on the SaaS side)
    saas_ws = args.workspace
    if not saas_ws:
        integs = get_slack_integrations(args.token, args.org)
        if not integs:
            sys.exit("No Slack integration installed on the SaaS org. Install it first "
                     "(same workspace), then re-run.")
        if len(integs) > 1:
            print("Multiple Slack integrations found; pass --workspace <id>:")
            for i in integs:
                print(f"  id={i['id']} name={i.get('name')} domain={i.get('domainName')}")
            sys.exit(1)
        saas_ws = integs[0]["id"]
        print(f"Destination Slack integration: id={saas_ws} "
              f"name={integs[0].get('name')} domain={integs[0].get('domainName')}")

    # Rewrite ONLY the instance-specific workspace id; keep channel (+ channel_id unless rebinding)
    new_action = dict(slack_action)
    new_action["workspace"] = str(saas_ws)
    new_action.pop("uuid", None)  # let SaaS assign a fresh one
    if args.rebind_channel:
        removed = new_action.pop("channel_id", None)
        print(f"--rebind-channel: dropped source channel_id={removed}; "
              f"SaaS will resolve by name {new_action.get('channel')!r}")

    payload = {
        "name": fields.get("label"),
        "actionMatch": blob.get("action_match", "any"),
        # filter_match may be present-but-null in the export; SaaS rejects null, so coerce to "all"
        "filterMatch": blob.get("filter_match") or "all",
        "frequency": blob.get("frequency", 30),
        "conditions": blob.get("conditions", []),
        "filters": blob.get("filters", []),
        "actions": [new_action],
    }

    print(f"\nCreating rule on project {slug} with REWRITTEN Slack action "
          f"(workspace {slack_action.get('workspace')} -> {saas_ws})...")
    result = create_rule(args.token, args.org, slug, payload, args.dry_run)
    if args.dry_run:
        return
    print("\nCreated rule:")
    print(f"  id      : {result.get('id')}")
    print(f"  name    : {result.get('name')}")
    print(f"  actions : {[a.get('id', '').split('.')[-1] for a in result.get('actions', [])]}")
    slack = next((a for a in result.get("actions", []) if a.get("id") == SLACK_ACTION_ID), None)
    if slack:
        print(f"  SLACK CARRIED OVER -> channel={slack.get('channel')} "
              f"workspace={slack.get('workspace')}")
    else:
        print("  (no Slack action on the created rule)")


if __name__ == "__main__":
    main()
