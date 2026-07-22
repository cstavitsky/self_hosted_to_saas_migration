"""Unit tests for issue-alert (sentry.rule) migration in core/migrate_alert_rules.py.

Hermetic: `requests` is stubbed before importing the module, so these run with plain
`python3` -- no network, no third-party deps. `requests.post` is recorded so we can
assert the exact payload/URL the migrator would send.

Run:  python3 -m unittest discover -s tests
  or: python3 tests/test_issue_alerts.py
"""
import json
import logging
import os
import sys
import tempfile
import types
import unittest

logging.disable(logging.CRITICAL)  # keep test output clean

# --- stub `requests` before importing the module under test ---
_POSTS = []  # each: {"url":..., "headers":..., "json":...}


class _FakeRequestException(Exception):
    pass


class _FakeResponse:
    def __init__(self, payload):
        self._payload = {"id": "new-id", "name": payload.get("name"), "echo": payload}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _fake_post(url, headers=None, json=None):
    _POSTS.append({"url": url, "headers": headers, "json": json})
    return _FakeResponse(json or {})


_fake_requests = types.ModuleType("requests")
_fake_requests.post = _fake_post
_fake_requests.exceptions = types.SimpleNamespace(RequestException=_FakeRequestException)
sys.modules["requests"] = _fake_requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core"))
import migrate_alert_rules as mar  # noqa: E402


def make_rule(pk=1, project=2, label="rule", owner_team=None, environment_id=None,
              action_match="any", filter_match="all", frequency=None,
              conditions=None, filters=None):
    blob = {"action_match": action_match, "filter_match": filter_match,
            "conditions": conditions if conditions is not None else [],
            "filters": filters if filters is not None else [],
            "actions": []}
    if frequency is not None:
        blob["frequency"] = frequency
    return {"model": "sentry.rule", "pk": pk, "fields": {
        "project": project, "environment_id": environment_id, "label": label,
        "owner_team": owner_team, "data": json.dumps(blob)}}


class IssueAlertMigrationTests(unittest.TestCase):
    def setUp(self):
        _POSTS.clear()
        self.m = mar.AlertRuleMigrator("tok", dry_run=False)
        self.project_slugs = {2: "checkout-service", 3: "payments-api"}
        self.team_map = {"50": "99123"}
        self.env_index = {1: "production"}

    def _run(self, rules, source_pk=None, user_map=None, slack_integration_id=None):
        migrated, failed, _, _ = self.m.migrate_issue_alerts(
            rules, "dest-org", self.project_slugs,
            self.team_map, self.env_index, source_pk=source_pk,
            user_map=user_map, slack_integration_id=slack_integration_id)
        return migrated, failed

    def _only_payload(self):
        self.assertEqual(len(_POSTS), 1, "expected exactly one POST")
        return _POSTS[0]

    # ---- happy paths ----
    def test_no_owner_defaults_to_issue_owners(self):
        migrated, failed = self._run([make_rule(owner_team=None)])
        self.assertEqual((len(migrated), len(failed)), (1, 0))
        payload = self._only_payload()["json"]
        self.assertEqual(payload["actions"], [{
            "id": "sentry.mail.actions.NotifyEmailAction",
            "targetType": "IssueOwners", "targetIdentifier": None,
            "fallthroughType": "ActiveMembers"}])
        self.assertNotIn("owner", payload)

    def test_owner_team_mapped_emails_team(self):
        migrated, failed = self._run([make_rule(owner_team=50)])
        self.assertEqual((len(migrated), len(failed)), (1, 0))
        payload = self._only_payload()["json"]
        self.assertEqual(payload["actions"][0]["targetType"], "Team")
        self.assertEqual(payload["actions"][0]["targetIdentifier"], "99123")
        self.assertEqual(payload["owner"], "team:99123")

    def test_owner_team_unmapped_falls_back(self):
        # owner team present in export but no mapping -> IssueOwners fallback, no owner
        migrated, failed = self._run([make_rule(owner_team=777)])
        self.assertEqual((len(migrated), len(failed)), (1, 0))
        payload = self._only_payload()["json"]
        self.assertEqual(payload["actions"][0]["targetType"], "IssueOwners")
        self.assertNotIn("owner", payload)

    def test_conditions_and_filters_preserved(self):
        conds = [{"id": "sentry.rules.conditions.first_seen_event.FirstSeenEventCondition"}]
        filts = [{"id": "sentry.rules.filters.age_comparison.AgeComparisonFilter",
                  "comparison_type": "older", "value": 10, "time": "minute"}]
        self._run([make_rule(conditions=conds, filters=filts)])
        payload = self._only_payload()["json"]
        self.assertEqual(payload["conditions"], conds)
        self.assertEqual(payload["filters"], filts)

    def test_match_modes_and_frequency(self):
        self._run([make_rule(action_match="all", filter_match="none", frequency=60)])
        payload = self._only_payload()["json"]
        self.assertEqual(payload["actionMatch"], "all")
        self.assertEqual(payload["filterMatch"], "none")
        self.assertEqual(payload["frequency"], 60)

    def test_frequency_defaults_to_30(self):
        self._run([make_rule(frequency=None)])
        self.assertEqual(self._only_payload()["json"]["frequency"], 30)

    def test_environment_mapped_and_null(self):
        self._run([make_rule(pk=1, environment_id=1)])
        self.assertEqual(self._only_payload()["json"]["environment"], "production")
        _POSTS.clear()
        self._run([make_rule(pk=2, environment_id=None)])
        self.assertIsNone(self._only_payload()["json"]["environment"])

    def test_url_is_project_rules_endpoint(self):
        self._run([make_rule(project=3)])
        self.assertEqual(self._only_payload()["url"],
                         "https://sentry.io/api/0/projects/dest-org/payments-api/rules/")

    def test_name_from_label(self):
        self._run([make_rule(label="Send a notification for high priority issues")])
        self.assertEqual(self._only_payload()["json"]["name"],
                         "Send a notification for high priority issues")

    # ---- error handling: no POST, recorded as failed ----
    def test_missing_project_is_failed_not_posted(self):
        migrated, failed = self._run([make_rule(project=999)])
        self.assertEqual((len(migrated), len(failed)), (0, 1))
        self.assertEqual(len(_POSTS), 0)

    def test_unparseable_data_is_failed_not_posted(self):
        bad = {"model": "sentry.rule", "pk": 5, "fields": {
            "project": 2, "environment_id": None, "label": "bad",
            "owner_team": None, "data": "{not valid json"}}
        migrated, failed = self._run([bad])
        self.assertEqual((len(migrated), len(failed)), (0, 1))
        self.assertEqual(len(_POSTS), 0)

    def test_non_rule_models_ignored(self):
        migrated, failed = self._run([{"model": "sentry.project", "pk": 2, "fields": {}}])
        self.assertEqual((len(migrated), len(failed), len(_POSTS)), (0, 0, 0))

    # ---- dry-run makes no network calls ----
    def test_dry_run_does_not_post(self):
        dry = mar.AlertRuleMigrator("tok", dry_run=True)
        migrated, failed, _, _ = dry.migrate_issue_alerts(
            [make_rule(owner_team=50)], "dest-org", self.project_slugs,
            self.team_map, self.env_index)
        self.assertEqual((len(migrated), len(failed)), (1, 0))
        self.assertEqual(len(_POSTS), 0)

    # ---- source-org scoping: other-org rules skipped, not failed ----
    def test_other_org_rule_skipped_not_failed(self):
        # project 999 is not in project_slugs; with source_pk set it is another org's rule.
        migrated, failed, skipped, _ = self.m.migrate_issue_alerts(
            [make_rule(project=999)], "dest-org", self.project_slugs,
            self.team_map, self.env_index, source_pk=4510189565050880)
        self.assertEqual((len(migrated), len(failed), len(skipped)), (0, 0, 1))
        self.assertEqual(len(_POSTS), 0)

    def test_no_source_pk_keeps_failure_semantics(self):
        # without source filtering, an unmapped project is still a failure (not skipped)
        migrated, failed, skipped, _ = self.m.migrate_issue_alerts(
            [make_rule(project=999)], "dest-org", self.project_slugs,
            self.team_map, self.env_index, source_pk=None)
        self.assertEqual((len(migrated), len(failed), len(skipped)), (0, 1, 0))

    # ---- full-fidelity action porting ----
    def _slack_action(self, channel="#alerts", channel_id="C123", workspace="1"):
        return {"id": "sentry.integrations.slack.notify_action.SlackNotifyServiceAction",
                "workspace": workspace, "channel": channel, "channel_id": channel_id,
                "tags": "", "uuid": "src-uuid"}

    def _email_action(self, target_type, target_id):
        return {"id": "sentry.mail.actions.NotifyEmailAction", "targetType": target_type,
                "targetIdentifier": target_id, "fallthroughType": "ActiveMembers", "uuid": "e-uuid"}

    def test_slack_workspace_swapped_channel_kept(self):
        rule = make_rule(owner_team=None)
        blob = json.loads(rule["fields"]["data"]); blob["actions"] = [self._slack_action()]
        rule["fields"]["data"] = json.dumps(blob)
        self._run([rule], slack_integration_id="55599")
        acts = self._only_payload()["json"]["actions"]
        self.assertEqual(len(acts), 1)
        self.assertEqual(acts[0]["workspace"], "55599")          # swapped
        self.assertEqual(acts[0]["channel_id"], "C123")          # preserved
        self.assertNotIn("uuid", acts[0])                        # dropped, SaaS reassigns

    def test_multiple_actions_all_preserved_and_remapped(self):
        rule = make_rule(owner_team=None)
        blob = json.loads(rule["fields"]["data"])
        blob["actions"] = [self._email_action("Team", 50),       # -> team_map["50"]="99123"
                           self._email_action("IssueOwners", ""),
                           self._slack_action(channel="#ops")]
        rule["fields"]["data"] = json.dumps(blob)
        migrated, failed, _, dropped = self.m.migrate_issue_alerts(
            [rule], "dest-org", self.project_slugs, self.team_map, self.env_index,
            user_map={}, slack_integration_id="55599")
        self.assertEqual((len(migrated), len(failed), len(dropped)), (1, 0, 0))
        acts = self._only_payload()["json"]["actions"]
        self.assertEqual(len(acts), 3)                           # all three preserved
        self.assertEqual(acts[0]["targetIdentifier"], "99123")   # team remapped
        self.assertEqual(acts[2]["workspace"], "55599")          # slack remapped

    def test_member_email_remapped_via_user_map(self):
        rule = make_rule(owner_team=None)
        blob = json.loads(rule["fields"]["data"]); blob["actions"] = [self._email_action("Member", 54)]
        rule["fields"]["data"] = json.dumps(blob)
        self._run([rule], user_map={"54": "15012091"})
        self.assertEqual(self._only_payload()["json"]["actions"][0]["targetIdentifier"], "15012091")

    def test_unportable_action_dropped_and_recorded(self):
        rule = make_rule(pk=9, owner_team=None)
        blob = json.loads(rule["fields"]["data"])
        blob["actions"] = [{"id": "sentry.integrations.pagerduty.notify_action.PagerDutyNotifyServiceAction",
                            "account": "2", "service": "24", "uuid": "pd"}]
        rule["fields"]["data"] = json.dumps(blob)
        migrated, failed, _, dropped = self.m.migrate_issue_alerts(
            [rule], "dest-org", self.project_slugs, self.team_map, self.env_index)
        self.assertEqual((len(migrated), len(failed)), (1, 0))    # rule still created
        self.assertEqual(len(dropped), 1)                        # PagerDuty recorded
        # rule had only the unportable action -> default email injected so SaaS accepts it
        acts = self._only_payload()["json"]["actions"]
        self.assertEqual(acts[0]["id"], "sentry.mail.actions.NotifyEmailAction")

    def test_pagerduty_account_and_service_remapped(self):
        rule = make_rule(owner_team=None)
        blob = json.loads(rule["fields"]["data"])
        blob["actions"] = [{"id": "sentry.integrations.pagerduty.notify_action.PagerDutyNotifyServiceAction",
                            "account": "2", "service": "24", "severity": "default", "uuid": "pd"}]
        rule["fields"]["data"] = json.dumps(blob)
        migrated, failed, _, dropped = self.m.migrate_issue_alerts(
            [rule], "dest-org", self.project_slugs, self.team_map, self.env_index,
            pd_account_map={"2": "987"}, pd_service_map={"24": "1055"})
        self.assertEqual((len(migrated), len(failed), len(dropped)), (1, 0, 0))
        act = self._only_payload()["json"]["actions"][0]
        self.assertEqual((act["account"], act["service"]), ("987", "1055"))
        self.assertEqual(act["severity"], "default")   # other fields preserved
        self.assertNotIn("uuid", act)

    def test_pagerduty_partial_map_is_dropped(self):
        # account maps but service does not -> drop (don't send a half-mapped action)
        rule = make_rule(pk=8, owner_team=50)
        blob = json.loads(rule["fields"]["data"])
        blob["actions"] = [{"id": "sentry.integrations.pagerduty.notify_action.PagerDutyNotifyServiceAction",
                            "account": "2", "service": "24", "uuid": "pd"}]
        rule["fields"]["data"] = json.dumps(blob)
        migrated, failed, _, dropped = self.m.migrate_issue_alerts(
            [rule], "dest-org", self.project_slugs, self.team_map, self.env_index,
            pd_account_map={"2": "987"}, pd_service_map={})
        self.assertEqual((len(migrated), len(dropped)), (1, 1))
        self.assertIn("service 24", dropped[0]["dropped"][0])

    def test_slack_dropped_when_no_integration_id(self):
        rule = make_rule(owner_team=50)
        blob = json.loads(rule["fields"]["data"]); blob["actions"] = [self._slack_action()]
        rule["fields"]["data"] = json.dumps(blob)
        migrated, failed, _, dropped = self.m.migrate_issue_alerts(
            [rule], "dest-org", self.project_slugs, self.team_map, self.env_index,
            slack_integration_id=None)
        self.assertEqual((len(migrated), len(dropped)), (1, 1))  # dropped + fell back
        self.assertEqual(self._only_payload()["json"]["actions"][0]["targetType"], "Team")


class MigrateAlertRulesIntegrationTests(unittest.TestCase):
    """Exercises the top-level orchestration: results shape + --skip-issue-alerts."""

    def setUp(self):
        _POSTS.clear()
        self.tmp = tempfile.mkdtemp()
        # export: one project + one issue alert, no metric alerts
        export = [
            {"model": "sentry.project", "pk": 2, "fields": {"slug": "checkout-service"}},
            make_rule(project=2, owner_team=50),
        ]
        self.export_file = os.path.join(self.tmp, "export.json")
        with open(self.export_file, "w") as f:
            json.dump(export, f)
        self.map_file = os.path.join(self.tmp, "map.json")
        with open(self.map_file, "w") as f:
            json.dump({"team_id_mappings": [{"old_pk": 50, "new_id": "99123"}]}, f)

    def test_results_shape_and_counts(self):
        m = mar.AlertRuleMigrator("tok", dry_run=False)
        res = m.migrate_alert_rules(self.export_file, "dest-org", self.map_file)
        self.assertEqual(set(res.keys()), {"metric", "issue"})
        self.assertEqual(len(res["metric"]["migrated"]), 0)
        self.assertEqual(len(res["issue"]["migrated"]), 1)
        self.assertEqual(len(_POSTS), 1)

    def test_skip_issue_alerts_flag(self):
        m = mar.AlertRuleMigrator("tok", dry_run=False)
        res = m.migrate_alert_rules(self.export_file, "dest-org", self.map_file,
                                    migrate_issue=False)
        self.assertEqual(len(res["issue"]["migrated"]), 0)
        self.assertEqual(len(_POSTS), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
