import json
import logging
import argparse
import re
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

# Metric-alert trigger-action enums, verified against Sentry source
# (src/sentry/notifications/models/notificationaction.py: ActionService / ActionTarget).
# The SaaS create API (POST /organizations/{org}/alert-rules/) wants string slugs, not these ints.
_METRIC_ACTION_TYPE = {0: "email", 1: "pagerduty", 2: "slack", 3: "msteams",
                       4: "sentry_app", 6: "opsgenie", 7: "discord"}  # 5=sentry_notification is exempt
_METRIC_TARGET_TYPE = {0: "specific", 1: "user", 2: "team", 3: "sentry_app"}  # 4=issue_owners n/a for metric

# AlertRule.Status.SNAPSHOT — an archival copy Sentry keeps when a metric alert is edited. These have
# no project/subscription by design and must never be migrated (they'd be projectless junk in SaaS).
ALERTRULE_STATUS_SNAPSHOT = 4

# --- Transaction -> span (EAP) metric-alert translation ------------------------------------
# SaaS has disabled creating transaction-dataset metric alerts; they must be recreated on the
# span dataset (events_analytics_platform) with an is_transaction:true filter.
#
# Translation rules confirmed against the Sentry source (src/sentry/search/eap/spans/ and
# src/sentry/snuba/*): the span dataset supports duration percentile aggregates on span.duration,
# count(span.duration), and failure_rate() (identical failure definition: sentry.status NOT IN
# ok/cancelled/unknown). Span search accepts the same `transaction:` and `tags[key]:` filters, plus
# `is_transaction:true`. Event type must be exactly ["trace_item_span"] and query_type = 1.
# We translate every aggregate with a confirmed span equivalent and only flag ones without one
# (web-vital measurements, custom percentile(), percentage(), etc.).
SPAN_DATASET = "events_analytics_platform"
TRANSACTION_DATASETS = ("transactions", "generic_metrics")
_DURATION_AGG = re.compile(r"^(avg|p50|p75|p90|p95|p99|p100)\(transaction\.duration\)$")


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
                    "actions": [],
                    # internal: the source trigger pk, used to attach its ported alertruletriggeraction
                    # actions in migrate_alert_rules(); stripped before the payload is sent.
                    "_src_trigger_pk": i.get("pk"),
                })
        return out

    def build_trigger_actions(self, data: List[Dict]) -> Dict[int, List[Dict]]:
        """alert_rule_trigger pk -> list of its source sentry.alertruletriggeraction field dicts.

        These carry the metric-alert notification targets (Slack/email/etc). Older/scrubbed exports
        may contain zero of these rows, in which case metric alerts fall back to a default owner email.
        """
        out: Dict[int, List[Dict]] = {}
        for i in data:
            if isinstance(i, dict) and i.get("model") == "sentry.alertruletriggeraction":
                f = i.get("fields", {})
                out.setdefault(f.get("alert_rule_trigger"), []).append(f)
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
                            team_new_id, pd_account_map=None, pd_service_map=None,
                            rebind_channel=False):
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
                if rebind_channel:
                    # Cross-workspace migration: source channel_id is invalid in the destination
                    # workspace, so drop it and let SaaS resolve by channel name instead.
                    out.pop("channel_id", None)
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

    def _port_metric_actions(self, source_actions, team_map, user_map,
                             slack_integration_id, rebind_channel=False):
        """Re-create a metric-alert trigger's actions on SaaS from the export's
        sentry.alertruletriggeraction rows. Mirrors _port_issue_actions but uses the metric-alert
        trigger-action schema (verified against Sentry source), which differs from issue rules:
          email -> {type:email,  targetType:user|team, targetIdentifier:<mapped id>}
          slack -> {type:slack,  targetType:specific,  targetIdentifier:'#channel',
                    integrationId:<slack_integration_id>[, inputChannelId:<raw id>]}
        Slack integrationId is remapped to the destination via --slack-integration-id. With
        rebind_channel the source channel id is dropped so SaaS resolves by name (cross-workspace).
        Unmappable actions (unknown target, missing integration/app id, raw-email 'specific') are
        dropped and recorded rather than sent with a stale id that would 400 the whole rule.
        Returns (ported, dropped)."""
        ported, dropped = [], []
        for a in source_actions or []:
            slug = _METRIC_ACTION_TYPE.get(a.get("type"))
            if slug == "email":
                tt = _METRIC_TARGET_TYPE.get(a.get("target_type"))
                ident = a.get("target_identifier")
                if tt == "team":
                    new = team_map.get(str(ident))
                    if new is None:
                        dropped.append(f"email->Team {ident} (no team mapping)"); continue
                    ported.append({"type": "email", "targetType": "team", "targetIdentifier": str(new)})
                elif tt == "user":
                    new = user_map.get(str(ident)) if user_map else None
                    if new is None:
                        dropped.append(f"email->Member {ident} (no user mapping)"); continue
                    ported.append({"type": "email", "targetType": "user", "targetIdentifier": str(new)})
                else:
                    # SaaS email handler supports only user/team targets (not a raw 'specific' address).
                    dropped.append(f"email->{tt or a.get('target_type')} (unsupported target for metric email)")
            elif slug == "slack":
                if not slack_integration_id:
                    dropped.append(f"Slack {a.get('target_display')} (no --slack-integration-id given)"); continue
                act = {"type": "slack", "targetType": "specific",
                       "targetIdentifier": a.get("target_display"),  # e.g. '#alerts'
                       "integrationId": int(slack_integration_id)}
                if not rebind_channel and a.get("target_identifier"):
                    # Same-workspace: keep the resolved channel id so SaaS validates synchronously
                    # (201) instead of the name-lookup async path (202).
                    act["inputChannelId"] = a.get("target_identifier")
                ported.append(act)
            elif slug in ("pagerduty", "opsgenie", "msteams", "discord", "sentry_app"):
                dropped.append(f"{slug} (integration/app action, no SaaS mapping provided)")
            else:
                dropped.append(f"action type {a.get('type')} (unrecognized)")
        return ported, dropped

    def migrate_issue_alerts(self, data: List[Dict], org_slug: str,
                             project_slugs: Dict[int, str], team_map: Dict[str, str],
                             env_index: Dict[int, str], only_names=None, source_pk=None,
                             user_map=None, slack_integration_id=None,
                             pd_account_map=None, pd_service_map=None, rebind_channel=False,
                             skip_project_slugs=None):
        """Recreate sentry.rule issue alerts via the project rules endpoint."""
        skip_project_slugs = set(skip_project_slugs or [])
        migrated, failed, skipped_other_org, dropped_actions = [], [], [], []
        skipped_existing_project = []
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
            # Merge mode: skip alerts on a project that already exists from a prior instance so we
            # don't pile this instance's alerts onto the existing (first-instance) project.
            if project_slug in skip_project_slugs:
                skipped_existing_project.append({"pk": pk, "name": name, "project": project_slug})
                logger.info(f"Issue alert {pk} '{name}': skipped (project '{project_slug}' already exists — merge collision)")
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
                pd_account_map=pd_account_map, pd_service_map=pd_service_map,
                rebind_channel=rebind_channel)
            if dropped:
                dropped_actions.append({"pk": pk, "name": name, "dropped": dropped})
                logger.warning(f"Issue alert {pk} '{name}': dropped {len(dropped)} unportable "
                               f"action(s): {dropped}")

            # Self-hosted stores condition AND filter nodes together in `conditions`; SaaS's API
            # wants them in SEPARATE fields and rejects a `sentry.rules.filters.*` node submitted
            # under `conditions`. Partition by node id: filter nodes -> filters, the rest -> conditions.
            raw_conditions = blob.get("conditions", []) or []
            raw_filters = blob.get("filters", []) or []
            split_conditions = [c for c in raw_conditions if ".filters." not in (c.get("id") or "")]
            split_filters = raw_filters + [c for c in raw_conditions if ".filters." in (c.get("id") or "")]

            payload = {
                "name": name,
                "actionMatch": blob.get("action_match", "any"),
                # filter_match may be present-but-null in the export (rules with no filters); SaaS
                # rejects null, so coerce to "all" (harmless with an empty filter list).
                "filterMatch": blob.get("filter_match") or "all",
                "frequency": blob.get("frequency", 30),
                "environment": env_name,
                "conditions": split_conditions,
                "filters": split_filters,
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
        return migrated, failed, skipped_other_org, dropped_actions, skipped_existing_project

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

    def _slack_issue_alerts_in_scope(self, data, project_slugs, source_pk, only_names):
        """Return the names of in-scope issue alerts (sentry.rule) that notify Slack.

        Mirrors the filtering in migrate_issue_alerts (--only and --source-org scope) so the
        result reflects exactly what THIS run would migrate, not the whole export file.
        """
        hits = []
        for item in data:
            if not isinstance(item, dict) or item.get("model") != "sentry.rule":
                continue
            fields = item.get("fields", {})
            name = fields.get("label")
            if only_names is not None and name not in only_names:
                continue
            if source_pk is not None and fields.get("project") not in project_slugs:
                continue
            raw = fields.get("data")
            try:
                blob = json.loads(raw) if isinstance(raw, str) else (raw or {})
            except (ValueError, TypeError):
                continue
            if any("slack" in (a.get("id") or "").lower() for a in (blob.get("actions") or [])):
                hits.append(name)
        return hits

    @staticmethod
    def translate_transaction_snuba(snuba):
        """Translate a transaction/generic_metrics metric-alert query to the span (EAP) dataset.

        Returns (overrides, None) when the alert can be translated, where `overrides` is a dict of
        payload fields to replace. Returns (None, reason) to FLAG an alert that has no confirmed
        span-dataset equivalent (web-vital measurements, custom percentile(), percentage(), etc.).

        Confirmed against Sentry source: duration percentiles map transaction.duration->span.duration,
        count()->count(span.duration), and failure_rate() is valid as-is on spans (same failure
        definition). The existing query filters (transaction:, tags[...]) are valid span-search
        syntax and are carried over verbatim; we just add is_transaction:true to scope to transactions.
        """
        agg = (snuba.get("aggregate") or "").strip()
        query = (snuba.get("query") or "").strip()
        if _DURATION_AGG.match(agg):
            new_agg = agg.replace("transaction.duration", "span.duration")
        elif agg == "count()":
            new_agg = "count(span.duration)"
        elif agg == "failure_rate()":
            new_agg = "failure_rate()"  # valid span formula; identical failure semantics
        else:
            return None, f"aggregate has no confirmed span-dataset equivalent: {agg!r}"
        # Span search supports transaction:/tags[...] as-is; scope to transaction-segment spans.
        new_query = "is_transaction:true" if not query else f"{query} is_transaction:true"
        return {
            "dataset": SPAN_DATASET,
            "aggregate": new_agg,
            "query": new_query,
            "queryType": 1,
            "eventTypes": ["trace_item_span"],
        }, None

    def migrate_alert_rules(self, export_file: str, org_slug: str, team_mappings_file: str,
                            source_org: str = None, migrate_issue: bool = True, only_names=None,
                            user_mappings_file: str = None, slack_integration_id=None,
                            pd_account_map=None, pd_service_map=None,
                            override_slack_with_email: bool = False, rebind_channel: bool = False,
                            skip_project_slugs=None):
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

        # Guard: if any in-scope issue alert notifies Slack, refuse to run unless the caller either
        # supplies a Slack integration id (to preserve those actions) or explicitly opts in to
        # replacing them with the default owner-team email. Enforced in dry-run AND live so it fails
        # fast. Only relevant when issue alerts are being migrated (Slack actions live on them).
        if migrate_issue and not slack_integration_id and not override_slack_with_email:
            slack_alerts = self._slack_issue_alerts_in_scope(data, project_slugs, source_pk, only_names)
            if slack_alerts:
                sample = ", ".join(sorted(set(slack_alerts))[:5])
                more = "" if len(set(slack_alerts)) <= 5 else f", +{len(set(slack_alerts)) - 5} more"
                raise SystemExit(
                    f"ERROR: {len(slack_alerts)} issue alert(s) in scope send Slack notifications "
                    f"(e.g. {sample}{more}).\n"
                    f"Re-run with --slack-integration-id <id> to preserve the Slack actions, or "
                    f"--override_slack_notifications_with_email=true to replace them with the default "
                    f"owner-team email."
                )

        rule_projects = self.build_rule_projects(data)
        rule_triggers = self.build_rule_triggers(data)
        trigger_actions = self.build_trigger_actions(data)
        env_index = self.build_environments(data)

        skip_project_slugs = set(skip_project_slugs or [])
        migrated_rules, failed_rules, skipped_other_org, flagged_transaction = [], [], [], []
        skipped_existing_project = []
        metric_dropped_actions = []
        skipped_snapshot = []

        for item in data:
            if not isinstance(item, dict) or item.get("model") != "sentry.alertrule":
                continue
            pk = item.get("pk")
            fields = item.get("fields", {})
            name = fields.get("name")

            if only_names is not None and name not in only_names:
                continue

            # Skip alert-rule SNAPSHOTS: archival copies (status=4) with no project/subscription.
            # They can't and shouldn't migrate; count them as skipped rather than failed.
            if fields.get("status") == ALERTRULE_STATUS_SNAPSHOT:
                skipped_snapshot.append({"pk": pk, "name": name})
                logger.info(f"Alert rule {pk} '{name}': skipped (snapshot — archival copy, no project)")
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
            # Merge mode: don't attach a metric alert to a project that already exists from a prior
            # instance (first instance wins). Drop colliding projects; skip the rule if none remain.
            if skip_project_slugs:
                kept = [s for s in projects if s not in skip_project_slugs]
                if not kept:
                    skipped_existing_project.append({"pk": pk, "name": name, "projects": projects})
                    logger.info(f"Alert rule {pk} '{name}': skipped (project(s) already exist — merge collision)")
                    continue
                projects = kept

            # owner: map source team pk -> new SaaS team id (or user_id -> new SaaS user id)
            team_pk = fields.get("team")
            team_new_id = team_map.get(str(team_pk)) if team_pk is not None else None
            if team_pk is not None and team_new_id is None:
                logger.warning(f"Alert rule {pk}: no team mapping for owner team pk {team_pk}; creating without owner")
            # Some metric alerts are owned by a USER, not a team. Resolve via user mappings so the
            # default action gets a real target instead of null (SaaS rejects a null targetIdentifier).
            user_pk = fields.get("user_id")
            user_new_id = user_map.get(str(user_pk)) if (user_pk is not None and user_map) else None
            if team_new_id is None and user_pk is not None and user_new_id is None:
                logger.warning(f"Alert rule {pk}: user-owned (user_id {user_pk}) but no user mapping; "
                               f"pass --user-mappings so its default action has a valid target")

            # triggers: real thresholds from alertruletrigger (fallback to a critical trigger)
            triggers = rule_triggers.get(pk) or [{"label": "critical", "alertThreshold": 100,
                                                  "actions": [], "_src_trigger_pk": None}]

            # Per trigger, PORT its real notification actions from the export's
            # sentry.alertruletriggeraction rows (Slack/email/etc). SaaS requires >=1 action per
            # trigger; if none port (or the export carries none — older/scrubbed exports), fall back
            # to a default owner-team/user email so the rule is still created.
            if team_new_id is not None:
                default_action = {"type": "email", "targetType": "team", "targetIdentifier": str(team_new_id)}
            elif user_new_id is not None:
                default_action = {"type": "email", "targetType": "user", "targetIdentifier": str(user_new_id)}
            else:
                default_action = {"type": "email", "targetType": "user", "targetIdentifier": None}
            clean_triggers, rule_dropped = [], []
            for t in triggers:
                ported, dropped = self._port_metric_actions(
                    trigger_actions.get(t.get("_src_trigger_pk"), []),
                    team_map, user_map, slack_integration_id, rebind_channel)
                rule_dropped.extend(dropped)
                if not ported:
                    ported = [default_action]
                clean_triggers.append({
                    "label": t.get("label", "critical"),
                    "alertThreshold": t.get("alertThreshold", 100),
                    "actions": ported,  # note: _src_trigger_pk is intentionally NOT sent
                })
            triggers = clean_triggers
            if rule_dropped:
                metric_dropped_actions.append({"pk": pk, "name": name, "dropped": rule_dropped})
                logger.warning(f"Alert rule {pk} '{name}': dropped {len(rule_dropped)} unportable "
                               f"action(s): {rule_dropped}")

            # Effective query fields — start from the source snuba query.
            eff_dataset = snuba.get("dataset", "events")
            eff_query = snuba.get("query", "")
            eff_aggregate = snuba.get("aggregate", "count()")
            eff_query_type = snuba.get("type", 0)
            eff_event_types = event_types.get(snuba_id, ["error"])

            # Transaction-based metric alerts can no longer be created on SaaS — translate the clean
            # cases to the span (EAP) dataset, and flag the rest (failure_rate/filtered) for manual rebuild.
            if eff_dataset in TRANSACTION_DATASETS:
                overrides, reason = self.translate_transaction_snuba(snuba)
                if overrides is None:
                    flagged_transaction.append({"pk": pk, "name": name, "aggregate": snuba.get("aggregate"),
                                                "query": snuba.get("query"), "reason": reason})
                    logger.warning(f"Alert rule {pk} '{name}': transaction-based, flagged for MANUAL span "
                                   f"rebuild ({reason})")
                    continue
                logger.info(f"Alert rule {pk} '{name}': translated transaction->span "
                            f"({snuba.get('aggregate')} on {snuba.get('dataset')} -> {overrides['aggregate']} "
                            f"on {overrides['dataset']}, +is_transaction:true) — VERIFY on a dry run")
                eff_dataset = overrides["dataset"]; eff_query = overrides["query"]
                eff_aggregate = overrides["aggregate"]; eff_query_type = overrides["queryType"]
                eff_event_types = overrides["eventTypes"]

            # Any span/EAP alert (translated OR already native) must use span event types; the source
            # event-type codes don't map to valid EAP values (that's why native EAP alerts failed before).
            if eff_dataset == SPAN_DATASET:
                eff_event_types = ["trace_item_span"]
                eff_query_type = 1

            payload = {
                "name": name,
                "dataset": eff_dataset,
                "query": eff_query,
                "aggregate": eff_aggregate,
                "timeWindow": snuba.get("time_window", 3600) // 60 if isinstance(snuba.get("time_window"), int) else 60,
                "queryType": eff_query_type,
                "eventTypes": eff_event_types,
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
        issue_skipped_existing = []
        if migrate_issue:
            issue_migrated, issue_failed, issue_skipped_other_org, issue_dropped_actions, issue_skipped_existing = \
                self.migrate_issue_alerts(
                    data, org_slug, project_slugs, team_map, env_index, only_names,
                    source_pk=source_pk, user_map=user_map, slack_integration_id=slack_integration_id,
                    pd_account_map=pd_account_map, pd_service_map=pd_service_map,
                    rebind_channel=rebind_channel, skip_project_slugs=skip_project_slugs
                )

        return {
            "metric": {"migrated": migrated_rules, "failed": failed_rules,
                       "skipped_other_org": skipped_other_org,
                       "skipped_existing_project": skipped_existing_project,
                       "flagged_transaction_manual": flagged_transaction,
                       "dropped_actions": metric_dropped_actions,
                       "skipped_snapshot": skipped_snapshot},
            "issue": {"migrated": issue_migrated, "failed": issue_failed,
                      "skipped_other_org": issue_skipped_other_org,
                      "skipped_existing_project": issue_skipped_existing,
                      "dropped_actions": issue_dropped_actions},
        }


def _load_skip_project_slugs(path):
    """Merge mode: load project slugs to skip. Accepts a collision_skip_projects_*.json
    (uses its 'projects_skip' key) or a plain JSON list. Empty set if no path given."""
    if not path:
        return set()
    with open(path) as f:
        d = json.load(f)
    slugs = d.get("projects_skip", []) if isinstance(d, dict) else d
    return set(slugs or [])


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
    parser.add_argument('--target-alert', dest='target_alert', action='append', metavar='NAME',
                        help='Only migrate alerts whose name/label exactly matches (repeatable)')
    parser.add_argument('--slack-integration-id', metavar='ID',
                        help="Destination SaaS Slack integration id. When set, Slack actions on BOTH "
                             "issue alerts and metric alerts are recreated against this integration. "
                             "Channel ids are kept as-is (same workspace) unless --rebind-channel is "
                             "given, which resolves by channel name instead (cross-workspace).")
    parser.add_argument('--rebind-channel', action='store_true',
                        help="Drop each Slack action's source channel_id so SaaS resolves by channel "
                             "NAME instead. ONLY for migrating into a DIFFERENT Slack workspace than "
                             "the source (e.g. a dummy test workspace). Leave OFF for production "
                             "(same workspace), where the original channel_id is valid and more "
                             "reliable (survives renames, avoids name-lookup timeouts).")
    parser.add_argument('--override_slack_notifications_with_email',
                        type=lambda v: str(v).strip().lower() in ('true', '1', 'yes', 'y'),
                        default=False, metavar='true|false',
                        help='If any in-scope issue alert notifies Slack, the script errors out '
                             'unless you pass --slack-integration-id. Set this true to instead '
                             'replace those Slack actions with the default owner-team email.')
    parser.add_argument('--user-mappings', metavar='FILE',
                        help='user_mappings_for_teams_<tag>.json from add_sentry_members.py. Used to '
                             'remap member-targeted email actions on issue alerts.')
    parser.add_argument('--skip-existing-projects', metavar='FILE',
                        help='Merge mode: collision_skip_projects_*.json (or a JSON list of slugs). Alerts '
                             'whose project already exists from a prior instance are SKIPPED so this '
                             'instance\'s alerts are not piled onto the existing project.')
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

    only_names = set(args.target_alert) if args.target_alert else None
    migrator = AlertRuleMigrator(args.auth_token, dry_run=dry_run)
    results = migrator.migrate_alert_rules(
        args.export_file, args.org_slug, args.team_mappings_file,
        source_org=args.source_org, migrate_issue=not args.skip_issue_alerts, only_names=only_names,
        user_mappings_file=args.user_mappings, slack_integration_id=args.slack_integration_id,
        pd_account_map=pd_account_map, pd_service_map=pd_service_map,
        override_slack_with_email=args.override_slack_notifications_with_email,
        rebind_channel=args.rebind_channel,
        skip_project_slugs=_load_skip_project_slugs(args.skip_existing_projects),
    )

    # Tag output with source org, dest org, and timestamp so per-org runs never overwrite.
    tag = f"{args.source_org or 'allorgs'}_{args.org_slug}_{datetime.now():%Y%m%d_%H%M%S}"
    with open(f"alert_rule_migration_results_{tag}.json", 'w') as f:
        json.dump(results, f, indent=2)

    m, i = results["metric"], results["issue"]
    logger.info(
        f"Completed. Metric alerts migrated: {len(m['migrated'])}, failed: {len(m['failed'])}, "
        f"skipped (other org): {len(m['skipped_other_org'])}, "
        f"skipped (existing project / merge): {len(m.get('skipped_existing_project', []))}, "
        f"flagged for manual span rebuild: {len(m.get('flagged_transaction_manual', []))}, "
        f"skipped (snapshot): {len(m.get('skipped_snapshot', []))}, "
        f"rules with dropped actions: {len(m.get('dropped_actions', []))} | "
        f"Issue alerts migrated: {len(i['migrated'])}, failed: {len(i['failed'])}, "
        f"skipped (other org): {len(i['skipped_other_org'])}, "
        f"skipped (existing project / merge): {len(i.get('skipped_existing_project', []))}, "
        f"rules with dropped actions: {len(i['dropped_actions'])}"
    )
    flagged = m.get("flagged_transaction_manual", [])
    if flagged:
        logger.info(f"  {len(flagged)} transaction alert(s) need manual rebuild as span-based:")
        for fa in flagged:
            logger.info(f"    - '{fa['name']}': {fa['reason']}")


import os as _rl_os, sys as _rl_sys
_rl_sys.path.insert(0, _rl_os.path.join(_rl_os.path.dirname(_rl_os.path.abspath(__file__)), "..", "common"))
_rl_sys.path.insert(0, _rl_os.path.join(_rl_os.path.dirname(_rl_os.path.abspath(__file__)), "common"))
from run_logging import start_run_log


if __name__ == "__main__":
    start_run_log("migrate_alert_rules")
    main()
