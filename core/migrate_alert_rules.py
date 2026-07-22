import json
import logging
import argparse
import requests
from typing import Dict, List
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Handles both alert types:
#   - METRIC alerts (sentry.alertrule)  -> /organizations/{org}/alert-rules/
#   - ISSUE  alerts (sentry.rule)       -> /projects/{org}/{proj}/rules/
# For issue alerts the original conditions/filters (which use stable, cross-instance
# rule-class ids) are carried over, but the notification actions are NOT in a portable
# form, so -- like metric alerts -- we inject a default "email the owner team" action
# (falling back to IssueOwners/ActiveMembers when a rule has no owner team).

# SnubaQueryEventType.EventType -> API eventTypes string
EVENT_TYPE_MAP = {0: "error", 1: "default", 2: "transaction"}


class AlertRuleMigrator:
    def __init__(self, auth_token: str, base_url: str = "https://sentry.io/api/0", dry_run: bool = False):
        self.auth_token = auth_token
        self.base_url = base_url
        self.dry_run = dry_run
        self.headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json"
        }

    def load_export_data(self, export_file: str) -> List[Dict]:
        with open(export_file, 'r') as f:
            return json.load(f)

    @staticmethod
    def resolve_source_org_pk(data: List[Dict], source_org: str = None):
        """Resolve which SOURCE org's rules to migrate. An export may contain many orgs. Alert rules
        are scoped by project membership (project->organization FK), so this returns the source org
        pk used to restrict which projects (and therefore which rules) are in scope. Returns None
        (no filter) only when the file holds a single org and no --source-org was given."""
        orgs = {i.get('pk'): (i.get('fields') or {}).get('slug')
                for i in data if isinstance(i, dict) and i.get('model') == 'sentry.organization'}
        if source_org:
            matches = [pk for pk, slug in orgs.items() if slug == source_org]
            if not matches:
                raise ValueError(f"--source-org '{source_org}' not found. Orgs in file: {sorted(filter(None, orgs.values()))}")
            if len(matches) > 1:
                raise ValueError(f"--source-org '{source_org}' is ambiguous (pks {matches} share this slug)")
            return matches[0]
        if len(orgs) > 1:
            raise ValueError(
                f"Export contains {len(orgs)} orgs {sorted(filter(None, orgs.values()))}; "
                f"pass --source-org SLUG to migrate one at a time.")
        return next(iter(orgs), None)

    # ---- lookup builders from the export ----
    def build_snuba_index(self, data: List[Dict]) -> Dict[int, Dict]:
        return {i["pk"]: i.get("fields", {}) for i in data
                if isinstance(i, dict) and i.get("model") == "sentry.snubaquery"}

    def build_event_types(self, data: List[Dict]) -> Dict[int, List[str]]:
        out: Dict[int, List[str]] = {}
        for i in data:
            if isinstance(i, dict) and i.get("model") == "sentry.snubaqueryeventtype":
                f = i.get("fields", {})
                sq = f.get("snuba_query")
                et = EVENT_TYPE_MAP.get(f.get("type"))
                if sq is not None and et:
                    out.setdefault(sq, []).append(et)
        return out

    def build_project_slugs(self, data: List[Dict], source_pk=None) -> Dict[int, str]:
        """Map project pk -> slug. When source_pk is given, only projects owned by that org are
        included, which is what scopes alert rules to a single source org."""
        out: Dict[int, str] = {}
        for i in data:
            if not isinstance(i, dict) or i.get("model") != "sentry.project":
                continue
            f = i.get("fields", {}) or {}
            if source_pk is not None and f.get("organization") != source_pk:
                continue
            out[i["pk"]] = f.get("slug")
        return out

    def build_environments(self, data: List[Dict]) -> Dict[int, str]:
        """environment pk -> name (SaaS rules take an environment name or null)."""
        return {
            i["pk"]: i.get("fields", {}).get("name")
            for i in data if isinstance(i, dict) and i.get("model") == "sentry.environment"
        }

    def build_rule_projects(self, data: List[Dict]) -> Dict[int, List[int]]:
        out: Dict[int, List[int]] = {}
        for i in data:
            if isinstance(i, dict) and i.get("model") == "sentry.alertruleprojects":
                f = i.get("fields", {})
                out.setdefault(f.get("alert_rule"), []).append(f.get("project"))
        return out

    def build_rule_triggers(self, data: List[Dict]) -> Dict[int, List[Dict]]:
        out: Dict[int, List[Dict]] = {}
        for i in data:
            if isinstance(i, dict) and i.get("model") == "sentry.alertruletrigger":
                f = i.get("fields", {})
                out.setdefault(f.get("alert_rule"), []).append({
                    "label": f.get("label", "critical"),
                    "alertThreshold": f.get("alert_threshold", 100),
                    "actions": [],  # NOTE: trigger actions/notifications are not carried over
                })
        return out

    def create_alert_rule(self, org_slug: str, payload: Dict) -> Dict:
        url = f"{self.base_url}/organizations/{org_slug}/alert-rules/"
        if self.dry_run:
            logger.info(f"[DRY-RUN] POST {url} payload={json.dumps(payload)}")
            return {"id": "dry-run", "name": payload.get("name"), "dry_run": True}
        try:
            response = requests.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to create alert rule: {str(e)}")
            if hasattr(e, "response") and e.response is not None and hasattr(e.response, "text"):
                logger.error(f"Response: {e.response.text}")
            raise

    def create_issue_alert_rule(self, org_slug: str, project_slug: str, payload: Dict) -> Dict:
        url = f"{self.base_url}/projects/{org_slug}/{project_slug}/rules/"
        if self.dry_run:
            logger.info(f"[DRY-RUN] POST {url} payload={json.dumps(payload)}")
            return {"id": "dry-run", "name": payload.get("name"), "project": project_slug, "dry_run": True}
        try:
            response = requests.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to create issue alert rule: {str(e)}")
            if hasattr(e, "response") and e.response is not None and hasattr(e.response, "text"):
                logger.error(f"Response: {e.response.text}")
            raise

    @staticmethod
    def _default_issue_action(team_new_id) -> Dict:
        """Default notification for a migrated issue alert: email the owner team, or
        fall back to the issue's suggested owners when no team maps."""
        if team_new_id is not None:
            return {
                "id": "sentry.mail.actions.NotifyEmailAction",
                "targetType": "Team",
                "targetIdentifier": str(team_new_id),
                "fallthroughType": "ActiveMembers",
            }
        return {
            "id": "sentry.mail.actions.NotifyEmailAction",
            "targetType": "IssueOwners",
            "targetIdentifier": None,
            "fallthroughType": "ActiveMembers",
        }

    def _port_issue_actions(self, source_actions, team_map, user_map, slack_integration_id,
                            team_new_id, pd_account_map=None, pd_service_map=None):
        """Re-create a rule's action list on SaaS, preserving every action and remapping the
        instance-specific ids each one carries:
          - email -> Team:        source team pk    -> mapped SaaS team id (team_map)
          - email -> Member:      source user id    -> mapped SaaS user id (user_map)
          - email -> IssueOwners: portable as-is
          - Slack:                'workspace' (source integration id) -> slack_integration_id
          - PagerDuty:            'account'/'service' -> mapped SaaS ids (pd_account_map/pd_service_map)
        Actions whose id can't be resolved (no mapping given) are dropped and recorded rather than
        sent with a stale id that would make SaaS reject the whole rule. If nothing survives, a
        default owner-team/IssueOwners email is injected so the rule is still created.
        Returns (actions, dropped) — dropped is a list of human-readable reasons.
        """
        pd_account_map = pd_account_map or {}
        pd_service_map = pd_service_map or {}
        ported, dropped = [], []
        for a in source_actions or []:
            aid = a.get("id", "")
            out = {k: v for k, v in a.items() if k != "uuid"}  # SaaS assigns a fresh uuid
            if aid.endswith("NotifyEmailAction"):
                tt = a.get("targetType")
                if tt == "Team":
                    new = team_map.get(str(a.get("targetIdentifier")))
                    if new is None:
                        dropped.append(f"email->Team {a.get('targetIdentifier')} (no team mapping)")
                        continue
                    out["targetIdentifier"] = str(new)
                elif tt == "Member":
                    new = user_map.get(str(a.get("targetIdentifier"))) if user_map else None
                    if new is None:
                        dropped.append(f"email->Member {a.get('targetIdentifier')} (no user mapping)")
                        continue
                    out["targetIdentifier"] = str(new)
                # IssueOwners (or any other targetType) has no id to remap
                ported.append(out)
            elif "slack" in aid.lower():
                if not slack_integration_id:
                    dropped.append(f"Slack {a.get('channel')} (no --slack-integration-id given)")
                    continue
                out["workspace"] = str(slack_integration_id)
                ported.append(out)
            elif "pagerduty" in aid.lower():
                acct = pd_account_map.get(str(a.get("account")))
                svc = pd_service_map.get(str(a.get("service")))
                if acct is None or svc is None:
                    missing = ([f"account {a.get('account')}"] if acct is None else []) + \
                              ([f"service {a.get('service')}"] if svc is None else [])
                    dropped.append(f"PagerDuty ({', '.join(missing)} unmapped)")
                    continue
                out["account"] = str(acct)
                out["service"] = str(svc)
                ported.append(out)
            else:
                # MS Teams / Opsgenie / webhook etc. — needs a SaaS-side id we weren't given
                dropped.append(f"{aid.split('.')[-1]} (integration action, no SaaS mapping provided)")
        if not ported:
            ported = [self._default_issue_action(team_new_id)]
        return ported, dropped

    def migrate_issue_alerts(self, data: List[Dict], org_slug: str,
                             project_slugs: Dict[int, str], team_map: Dict[str, str],
                             env_index: Dict[int, str], only_names=None, source_pk=None,
                             user_map=None, slack_integration_id=None,
                             pd_account_map=None, pd_service_map=None):
        """Recreate sentry.rule issue alerts via the project rules endpoint."""
        migrated, failed, skipped_other_org, dropped_actions = [], [], [], []
        for item in data:
            if not isinstance(item, dict) or item.get("model") != "sentry.rule":
                continue
            pk = item.get("pk")
            fields = item.get("fields", {})
            name = fields.get("label")

            if only_names is not None and name not in only_names:
                continue

            project_pk = fields.get("project")
            # project_slugs is scoped to the source org; a rule whose project isn't in scope
            # belongs to another org -- skip it (don't count it as a failure).
            if source_pk is not None and project_pk not in project_slugs:
                skipped_other_org.append({"pk": pk, "name": name, "reason": "Rule belongs to another org"})
                continue
            project_slug = project_slugs.get(project_pk)
            if not project_slug:
                failed.append((pk, "No project mapping found for issue alert"))
                logger.error(f"Issue alert {pk}: no project slug for project pk {project_pk}")
                continue

            raw = fields.get("data")
            try:
                blob = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except (ValueError, TypeError):
                failed.append((pk, "Unparseable rule data blob"))
                logger.error(f"Issue alert {pk}: could not parse data blob")
                continue

            team_pk = fields.get("owner_team")
            team_new_id = team_map.get(str(team_pk)) if team_pk is not None else None
            if team_pk is not None and team_new_id is None:
                logger.warning(f"Issue alert {pk}: no team mapping for owner team pk {team_pk}; "
                               f"defaulting action to IssueOwners")

            env_name = env_index.get(fields.get("environment_id")) if fields.get("environment_id") else None

            actions, dropped = self._port_issue_actions(
                blob.get("actions", []), team_map, user_map, slack_integration_id, team_new_id,
                pd_account_map=pd_account_map, pd_service_map=pd_service_map)
            if dropped:
                dropped_actions.append({"pk": pk, "name": name, "dropped": dropped})
                logger.warning(f"Issue alert {pk} '{name}': dropped {len(dropped)} unportable "
                               f"action(s): {dropped}")

            payload = {
                "name": name,
                "actionMatch": blob.get("action_match", "any"),
                "filterMatch": blob.get("filter_match", "all"),
                "frequency": blob.get("frequency", 30),
                "environment": env_name,
                "conditions": blob.get("conditions", []),
                "filters": blob.get("filters", []),
                "actions": actions,
            }
            if team_new_id is not None:
                payload["owner"] = f"team:{team_new_id}"

            try:
                new_rule = self.create_issue_alert_rule(org_slug, project_slug, payload)
                migrated.append(new_rule)
                logger.info(f"Migrated issue alert '{name}' -> project {project_slug}")
            except Exception as e:
                failed.append((pk, str(e)))
                logger.error(f"Failed to migrate issue alert {pk}: {e}")
        return migrated, failed, skipped_other_org, dropped_actions

    def load_team_mappings(self, mappings_file: str) -> Dict[str, str]:
        """old team pk -> new SaaS team id, from project_team_sync_results.json."""
        with open(mappings_file, "r") as f:
            data = json.load(f)
        team_mappings = {}
        for mapping in data.get("team_id_mappings", []):
            old_pk = str(mapping.get("old_pk"))
            new_id = mapping.get("new_id")
            if old_pk and new_id:
                team_mappings[old_pk] = new_id
        logger.info(f"Loaded {len(team_mappings)} team mappings")
        return team_mappings

    def load_user_mappings(self, mappings_file: str) -> Dict[str, str]:
        """old user id -> new SaaS user id, from user_mappings_for_teams.json (add_sentry_members)."""
        with open(mappings_file, "r") as f:
            data = json.load(f)
        user_mappings = {str(k): str(v) for k, v in data.get("user_mappings", {}).items() if v}
        logger.info(f"Loaded {len(user_mappings)} user mappings")
        return user_mappings

    def migrate_alert_rules(self, export_file: str, org_slug: str, team_mappings_file: str,
                            source_org: str = None, migrate_issue: bool = True, only_names=None,
                            user_mappings_file: str = None, slack_integration_id=None,
                            pd_account_map=None, pd_service_map=None):
        data = self.load_export_data(export_file)
        source_pk = self.resolve_source_org_pk(data, source_org)
        if source_pk is not None:
            logger.info(f"Filtering to source org '{source_org or '(only org in file)'}' (pk {source_pk})")
        team_map = self.load_team_mappings(team_mappings_file)
        user_map = self.load_user_mappings(user_mappings_file) if user_mappings_file else {}

        snuba_index = self.build_snuba_index(data)
        event_types = self.build_event_types(data)
        # project_slugs is scoped to the source org; rules whose projects are all outside it are skipped.
        project_slugs = self.build_project_slugs(data, source_pk=source_pk)
        rule_projects = self.build_rule_projects(data)
        rule_triggers = self.build_rule_triggers(data)
        env_index = self.build_environments(data)

        migrated_rules, failed_rules, skipped_other_org = [], [], []

        for item in data:
            if not isinstance(item, dict) or item.get("model") != "sentry.alertrule":
                continue
            pk = item.get("pk")
            fields = item.get("fields", {})
            name = fields.get("name")

            if only_names is not None and name not in only_names:
                continue

            snuba_id = fields.get("snuba_query")
            snuba = snuba_index.get(snuba_id, {})
            if not snuba:
                failed_rules.append((pk, "No snuba_query found"))
                continue

            # projects: map source project pks -> slugs
            proj_pks = rule_projects.get(pk, [])
            # When filtering by source org, drop rules whose projects all belong to another org.
            if source_pk is not None:
                in_scope = [p for p in proj_pks if p in project_slugs]
                if proj_pks and not in_scope:
                    skipped_other_org.append({"pk": pk, "name": name, "reason": "Rule belongs to another org"})
                    continue
                proj_pks = in_scope
            projects = [project_slugs.get(p) for p in proj_pks if project_slugs.get(p)]
            if not projects:
                failed_rules.append((pk, "No project mapping found (alertruleprojects empty)"))
                continue

            # owner: map source team pk -> new SaaS team id
            team_pk = fields.get("team")
            team_new_id = team_map.get(str(team_pk)) if team_pk is not None else None
            if team_pk is not None and team_new_id is None:
                logger.warning(f"Alert rule {pk}: no team mapping for owner team pk {team_pk}; creating without owner")

            # triggers: real thresholds from alertruletrigger (fallback to a critical trigger)
            triggers = rule_triggers.get(pk) or [{"label": "critical", "alertThreshold": 100, "actions": []}]

            # SaaS requires every trigger to have >=1 action (self-hosted does not).
            # Original notification actions are NOT in the export, so we inject a default
            # email-to-owner-team action to satisfy the API. Flag for review.
            if team_new_id is not None:
                default_action = {"type": "email", "targetType": "team", "targetIdentifier": str(team_new_id)}
            else:
                default_action = {"type": "email", "targetType": "user", "targetIdentifier": None}
            for t in triggers:
                if not t.get("actions"):
                    t["actions"] = [default_action]

            payload = {
                "name": name,
                "dataset": snuba.get("dataset", "events"),
                "query": snuba.get("query", ""),
                "aggregate": snuba.get("aggregate", "count()"),
                "timeWindow": snuba.get("time_window", 3600) // 60 if isinstance(snuba.get("time_window"), int) else 60,
                "queryType": snuba.get("type", 0),
                "eventTypes": event_types.get(snuba_id, ["error"]),
                "thresholdType": fields.get("threshold_type", 0),
                "resolveThreshold": fields.get("resolve_threshold"),
                "comparisonDelta": fields.get("comparison_delta"),
                "triggers": triggers,
                "projects": projects,
            }
            if team_new_id is not None:
                payload["owner"] = f"team:{team_new_id}"

            try:
                new_rule = self.create_alert_rule(org_slug, payload)
                migrated_rules.append(new_rule)
                logger.info(f"Migrated alert rule '{name}' -> projects {projects}")
            except Exception as e:
                failed_rules.append((pk, str(e)))
                logger.error(f"Failed to migrate alert rule {pk}: {e}")

        issue_migrated, issue_failed, issue_skipped_other_org, issue_dropped_actions = [], [], [], []
        if migrate_issue:
            issue_migrated, issue_failed, issue_skipped_other_org, issue_dropped_actions = \
                self.migrate_issue_alerts(
                    data, org_slug, project_slugs, team_map, env_index, only_names,
                    source_pk=source_pk, user_map=user_map, slack_integration_id=slack_integration_id,
                    pd_account_map=pd_account_map, pd_service_map=pd_service_map
                )

        return {
            "metric": {"migrated": migrated_rules, "failed": failed_rules,
                       "skipped_other_org": skipped_other_org},
            "issue": {"migrated": issue_migrated, "failed": issue_failed,
                      "skipped_other_org": issue_skipped_other_org,
                      "dropped_actions": issue_dropped_actions},
        }


def main():
    parser = argparse.ArgumentParser(description='Migrate Sentry metric and issue alert rules')
    parser.add_argument('auth_token', help='Sentry auth token')
    parser.add_argument('org_slug', help='Destination SaaS organization slug')
    parser.add_argument('export_file', help='Path to export.json file')
    parser.add_argument('team_mappings_file', help='project_team_sync_results.json from create_sentry_teams.py')
    parser.add_argument('--source-org', help='Source org slug to migrate (required when the export holds multiple orgs)')
    parser.add_argument('--run_on_real_data', type=lambda v: str(v).strip().lower() in ('true', '1', 'yes', 'y'),
                        default=False, metavar='true|false',
                        help='Set to true to actually perform changes. Default false = dry-run.')
    parser.add_argument('--dry-run', action='store_true',
                        help='(default) Dry-run is on by default; accepted for compatibility and is a no-op.')
    parser.add_argument('--skip-issue-alerts', action='store_true',
                        help='Migrate metric alerts only (skip sentry.rule issue alerts)')
    parser.add_argument('--only', action='append', metavar='NAME',
                        help='Only migrate alerts whose name/label exactly matches (repeatable)')
    parser.add_argument('--slack-integration-id', metavar='ID',
                        help="Destination SaaS Slack integration id. When set, issue-alert Slack "
                             "actions are recreated with this integration (channel names/ids are "
                             "kept as-is, so this assumes the same Slack workspace).")
    parser.add_argument('--user-mappings', metavar='FILE',
                        help='user_mappings_for_teams_<tag>.json from add_sentry_members.py. Used to '
                             'remap member-targeted email actions on issue alerts.')
    parser.add_argument('--pagerduty-account', action='append', metavar='SRC:DEST', default=[],
                        help='Map a source PagerDuty account (integration) id to its SaaS id, e.g. '
                             '--pagerduty-account 2:987. Repeatable.')
    parser.add_argument('--pagerduty-service', action='append', metavar='SRC:DEST', default=[],
                        help='Map a source PagerDuty service id to its SaaS id, e.g. '
                             '--pagerduty-service 24:1055. Repeatable.')
    args = parser.parse_args()

    def _parse_pairs(pairs, label):
        out = {}
        for p in pairs:
            if ":" not in p:
                parser.error(f"--{label} expects SRC:DEST, got '{p}'")
            src, dest = p.split(":", 1)
            out[src.strip()] = dest.strip()
        return out

    pd_account_map = _parse_pairs(args.pagerduty_account, "pagerduty-account")
    pd_service_map = _parse_pairs(args.pagerduty_service, "pagerduty-service")

    dry_run = not args.run_on_real_data
    if dry_run:
        logger.info("=== DRY RUN (default): no changes will be made to SaaS. Pass --run_on_real_data=true to apply. ===")
    else:
        logger.info("=== EXECUTE: changes WILL be made to SaaS ===")

    only_names = set(args.only) if args.only else None
    migrator = AlertRuleMigrator(args.auth_token, dry_run=dry_run)
    results = migrator.migrate_alert_rules(
        args.export_file, args.org_slug, args.team_mappings_file,
        source_org=args.source_org, migrate_issue=not args.skip_issue_alerts, only_names=only_names,
        user_mappings_file=args.user_mappings, slack_integration_id=args.slack_integration_id,
        pd_account_map=pd_account_map, pd_service_map=pd_service_map,
    )

    # Tag output with source org, dest org, and timestamp so per-org runs never overwrite.
    tag = f"{args.source_org or 'allorgs'}_{args.org_slug}_{datetime.now():%Y%m%d_%H%M%S}"
    with open(f"alert_rule_migration_results_{tag}.json", 'w') as f:
        json.dump(results, f, indent=2)

    m, i = results["metric"], results["issue"]
    logger.info(
        f"Completed. Metric alerts migrated: {len(m['migrated'])}, failed: {len(m['failed'])}, "
        f"skipped (other org): {len(m['skipped_other_org'])} | "
        f"Issue alerts migrated: {len(i['migrated'])}, failed: {len(i['failed'])}, "
        f"skipped (other org): {len(i['skipped_other_org'])}, "
        f"rules with dropped actions: {len(i['dropped_actions'])}"
    )


import os as _rl_os, sys as _rl_sys
_rl_sys.path.insert(0, _rl_os.path.join(_rl_os.path.dirname(_rl_os.path.abspath(__file__)), "..", "common"))
_rl_sys.path.insert(0, _rl_os.path.join(_rl_os.path.dirname(_rl_os.path.abspath(__file__)), "common"))
from run_logging import start_run_log


if __name__ == "__main__":
    start_run_log("migrate_alert_rules")
    main()
