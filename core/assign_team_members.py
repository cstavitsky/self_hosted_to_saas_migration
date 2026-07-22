import json
import requests
import logging
from typing import Dict, List
from collections import defaultdict
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class SentryTeamMemberManager:
    def __init__(self, auth_token: str, base_url: str = "https://sentry.io/api/0", dry_run: bool = False):
        self.base_url = base_url
        self.dry_run = dry_run
        self.headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json"
        }

    def load_export_data(self, export_file_path: str) -> List[Dict]:
        try:
            with open(export_file_path, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON file: {str(e)}")
            raise

    def load_member_mappings(self, mappings_file_path: str) -> Dict:
        try:
            with open(mappings_file_path, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid member mappings file: {str(e)}")
            raise

    @staticmethod
    def resolve_source_org_pk(data: List[Dict], source_org: str = None):
        """Resolve which SOURCE org's teams to consider. An export may contain many orgs; team
        records carry an `organization` FK (the source org pk). Returns that pk, or None (no filter)
        only when the file holds a single org and no --source-org was given."""
        orgs = {item.get('pk'): (item.get('fields') or {}).get('slug')
                for item in data if isinstance(item, dict) and item.get('model') == 'sentry.organization'}
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

    def add_member_to_team(self, org_slug: str, member_id: str, team_slug: str) -> bool:
        """
        Add a member to a team
        """
        url = f"{self.base_url}/organizations/{org_slug}/members/{member_id}/teams/{team_slug}/"

        if self.dry_run:
            logger.info(f"[DRY-RUN] POST {url} (add member '{member_id}' to team '{team_slug}')")
            return True

        try:
            response = requests.post(url, headers=self.headers)
            if response.status_code in [201, 202, 204]:  # All successful states
                logger.info(f"Successfully added member {member_id} to team {team_slug}")
                return True
            response.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to add member {member_id} to team {team_slug}: {str(e)}")
            if hasattr(e.response, 'text'):
                logger.error(f"Response: {e.response.text}")
            return False

    def sync_team_members(self, export_file_path: str, mappings_file_path: str, org_slug: str,
                          source_org: str = None) -> Dict:
        """
        Sync team memberships using the member mappings and export data. When the export holds
        multiple orgs, `source_org` (a source org slug) restricts which teams are considered;
        combined with the per-org member mappings file, this scopes assignments to one org.
        """
        results = {
            'successful': [],
            'failed': [],
            'skipped': [],
            'skipped_other_org': [],  # membership rows whose team belongs to a different source org
            'team_mappings': defaultdict(list)
        }

        try:
            # Load member ID mappings
            member_mappings = self.load_member_mappings(mappings_file_path)
            successful_members = member_mappings['user_mappings']

            # Load export data
            data = self.load_export_data(export_file_path)
            source_pk = self.resolve_source_org_pk(data, source_org)
            if source_pk is not None:
                logger.info(f"Filtering to source org '{source_org or '(only org in file)'}' (pk {source_pk})")

            # Create team pk to slug mapping (scoped to the source org when filtering)
            team_slugs = {}
            for item in data:
                if not isinstance(item, dict):
                    continue
                if item.get('model') == 'sentry.team':
                    fields = item.get('fields', {}) or {}
                    if source_pk is not None and fields.get('organization') != source_pk:
                        continue
                    team_slugs[item['pk']] = fields.get('slug')

            # Process organization member team relationships
            for item in data:
                if not isinstance(item, dict):
                    continue
                if item.get('model') == 'sentry.organizationmemberteam':
                    fields = item.get('fields', {}) or {}
                    user_pk = str(fields.get('organizationmember'))
                    team_pk = fields.get('team')
                    
                    # Skip if we don't have mappings for either user or team
                    if not user_pk or not team_pk:
                        continue

                    # Scope to --source-org: team_slugs only holds this org's teams, so a team_pk
                    # that's absent belongs to a different org in a multi-org export. Ignore it here
                    # (it's migrated when that org is run) rather than mis-reporting it as a failed user.
                    if team_pk not in team_slugs:
                        results['skipped_other_org'].append({'user_pk': user_pk, 'team_pk': team_pk})
                        continue

                    if user_pk not in successful_members:
                        reason = 'User not successfully created in Sentry'
                        logger.warning(f"  SKIP: organizationmember pk {user_pk} -> team pk {team_pk}: "
                                       f"{reason} (not in the member mappings file)")
                        results['skipped'].append({
                            'user_pk': user_pk,
                            'team_pk': team_pk,
                            'reason': reason
                        })
                        continue

                    team_slug = team_slugs.get(team_pk)
                    if not team_slug:
                        reason = 'Team slug not found'
                        logger.warning(f"  SKIP: organizationmember pk {user_pk} -> team pk {team_pk}: "
                                       f"{reason} (team pk not among this org's exported teams)")
                        results['skipped'].append({
                            'user_pk': user_pk,
                            'team_pk': team_pk,
                            'reason': reason
                        })
                        continue

                    sentry_member_id = successful_members[user_pk]
                    
                    if self.add_member_to_team(org_slug, sentry_member_id, team_slug):
                        results['successful'].append({
                            'user_pk': user_pk,
                            'team_pk': team_pk,
                            'sentry_member_id': sentry_member_id,
                            'team_slug': team_slug
                        })
                        results['team_mappings'][team_slug].append(sentry_member_id)
                    else:
                        results['failed'].append({
                            'user_pk': user_pk,
                            'team_pk': team_pk,
                            'sentry_member_id': sentry_member_id,
                            'team_slug': team_slug
                        })

            # Convert defaultdict to regular dict for JSON serialization
            results['team_mappings'] = dict(results['team_mappings'])
            return results

        except Exception as e:
            logger.error(f"Team member sync failed: {str(e)}")
            raise

def main():
    import sys
    import argparse

    parser = argparse.ArgumentParser(description='Assign Sentry members to teams')
    parser.add_argument('auth_token', help='Sentry authentication token')
    parser.add_argument('org_slug', help='Destination SaaS organization slug')
    parser.add_argument('export_file', help='JSON export file path')
    parser.add_argument('mappings_file', help='user_mappings_for_teams.json from add_sentry_members.py')
    parser.add_argument('--source-org', help='Source org slug to migrate (required when the export holds multiple orgs)')
    parser.add_argument('--run_on_real_data', type=lambda v: str(v).strip().lower() in ('true', '1', 'yes', 'y'),
                        default=False, metavar='true|false',
                        help='Set to true to actually perform changes. Default false = dry-run.')
    parser.add_argument('--dry-run', action='store_true',
                        help='(default) Dry-run is on by default; accepted for compatibility and is a no-op.')
    args = parser.parse_args()

    auth_token = args.auth_token
    org_slug = args.org_slug
    export_file = args.export_file
    mappings_file = args.mappings_file

    try:
        dry_run = not args.run_on_real_data
        if dry_run:
            logger.info("=== DRY RUN (default): no changes will be made to SaaS. Pass --run_on_real_data=true to apply. ===")
        else:
            logger.info("=== EXECUTE: changes WILL be made to SaaS ===")
        manager = SentryTeamMemberManager(auth_token, dry_run=dry_run)
        results = manager.sync_team_members(export_file, mappings_file, org_slug, source_org=args.source_org)
        
        logger.info("Team member sync completed:")
        logger.info(f"Successful assignments: {len(results['successful'])}")
        logger.info(f"Failed assignments: {len(results['failed'])}")
        logger.info(f"Skipped assignments: {len(results['skipped'])}")
        if results['skipped']:
            from collections import Counter as _Counter
            for reason, count in _Counter(s['reason'] for s in results['skipped']).most_common():
                logger.info(f"    - {count} skipped: {reason}")
        if results['skipped_other_org']:
            logger.info(f"Ignored (team belongs to another org, not --source-org): "
                        f"{len(results['skipped_other_org'])}")

        # Tag output with source org, dest org, and timestamp so per-org runs never overwrite.
        tag = f"{args.source_org or 'allorgs'}_{args.org_slug}_{datetime.now():%Y%m%d_%H%M%S}"
        out = f"team_member_assignments_{tag}.json"
        with open(out, 'w') as f:
            json.dump(results, f, indent=2)
        logger.info(f"Wrote {out}")
            
    except Exception as e:
        logger.error(f"Script execution failed: {str(e)}")
        sys.exit(1)


import os as _rl_os, sys as _rl_sys
_rl_sys.path.insert(0, _rl_os.path.join(_rl_os.path.dirname(_rl_os.path.abspath(__file__)), "..", "common"))
_rl_sys.path.insert(0, _rl_os.path.join(_rl_os.path.dirname(_rl_os.path.abspath(__file__)), "common"))
from run_logging import start_run_log


if __name__ == "__main__":
    start_run_log("assign_team_members")
    main()
