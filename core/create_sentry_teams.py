import json
import requests
import logging
from typing import Set, Dict, List
from collections import defaultdict
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

#mapping requires teams to already exist
class SentryTeamProjectMapper:
    def __init__(self, auth_token: str, org_slug: str, base_url: str = "https://sentry.io/api/0", dry_run: bool = False):
        self.base_url = base_url
        self.org_slug = org_slug
        self.dry_run = dry_run
        self.headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json"
        }
        self._dry_run_team_id = 0

    def load_export_data(self, export_file_path: str) -> List[Dict]:
        """
        Load data from the export JSON file
        """
        try:
            with open(export_file_path, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON file: {str(e)}")
            raise
        except FileNotFoundError:
            logger.error(f"File not found: {export_file_path}")
            raise

    @staticmethod
    def resolve_source_org_pk(data: List[Dict], source_org: str = None):
        """Resolve which SOURCE org's records to migrate. An export may contain many orgs; team and
        project records carry an `organization` FK (the source org pk). Returns that pk, or None
        (no filter) only when the file holds a single org and no --source-org was given."""
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

    def extract_mappings(self, data: List[Dict], source_pk=None) -> Dict[str, Dict]:
        """
        Extract teams and projects with their relationships
        Returns:
        {
            'teams': {
                'team_pk': {'slug': 'team_slug', 'name': 'team_name'},
                ...
            },
            'projects': {
                'project_slug': {
                    'pk': 'project_pk',
                    'team_pks': ['team_pk1', 'team_pk2', ...]
                },
                ...
            }
        }
        """
        mappings = {
            'teams': {},
            'projects': {}
        }
        project_team_maps = defaultdict(set)  # Project PK to set of team PKs

        # First pass: collect all teams and projects, and build relationships
        for item in data:
            if not isinstance(item, dict):
                continue
            if item.get('model') == 'sentry.team':
                team_pk = item.get('pk')
                fields = item.get('fields', {}) or {}
                if source_pk is not None and fields.get('organization') != source_pk:
                    continue
                if team_pk:
                    mappings['teams'][team_pk] = {
                        'slug': fields.get('slug', '').lower().replace(' ', '-'),
                        'name': fields.get('name')
                    }
                    logger.debug(f"Found team: {fields.get('name')} (PK: {team_pk})")

            elif item.get('model') == 'sentry.project':
                fields = item.get('fields', {}) or {}
                if source_pk is not None and fields.get('organization') != source_pk:
                    continue
                project_slug = fields.get('slug')
                project_pk = item.get('pk')
                if project_slug and project_pk:
                    mappings['projects'][project_slug] = {
                        'pk': project_pk,
                        'team_pks': set()  # Will be filled with multiple team PKs
                    }
                    # If this project has a team field, add to the mapping
                    if 'team' in fields:
                        project_team_maps[project_pk].add(fields['team'])
                    logger.debug(f"Found project: {project_slug} (PK: {project_pk})")
            
            # Look for projectteam relationships
            elif item.get('model') == 'sentry.projectteam':
                fields = item.get('fields', {})
                project_pk = fields.get('project')
                team_pk = fields.get('team')
                if project_pk and team_pk:
                    project_team_maps[project_pk].add(team_pk)
                    logger.debug(f"Found project-team relationship: Project PK {project_pk} -> Team PK {team_pk}")

        # Second pass: update project team relationships
        for project_slug, project_data in mappings['projects'].items():
            project_pk = project_data['pk']
            if project_pk in project_team_maps:
                team_pks = project_team_maps[project_pk]
                # Filter out any team PKs that don't exist in our teams mapping
                valid_team_pks = [pk for pk in team_pks if pk in mappings['teams']]
                mappings['projects'][project_slug]['team_pks'] = valid_team_pks
                logger.debug(f"Mapped project {project_slug} to teams: {valid_team_pks}")

        return mappings

    def add_team_to_project(self, project_slug: str, team_slug: str) -> bool:
        """
        Add a team to a project using the Sentry API
        """
        url = f"{self.base_url}/projects/{self.org_slug}/{project_slug}/teams/{team_slug}/"

        if self.dry_run:
            logger.info(f"[DRY-RUN] POST {url} (attach team '{team_slug}' to project '{project_slug}')")
            return True

        try:
            response = requests.post(url, headers=self.headers)
            response.raise_for_status()
            logger.info(f"Successfully added team '{team_slug}' to project '{project_slug}'")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to add team '{team_slug}' to project '{project_slug}': {str(e)}")
            if hasattr(e.response, 'text'):
                logger.error(f"Response: {e.response.text}")
            return False

    def create_team(self, org_slug: str, team_name: str, team_slug: str = None) -> Dict:
        """
        Create a new team in the organization
        Returns both API response and original team PK
        """
        url = f"{self.base_url}/organizations/{org_slug}/teams/"
        payload = {"name": team_name}
        if team_slug:
            payload["slug"] = team_slug

        if self.dry_run:
            self._dry_run_team_id += 1
            fake_id = f"dryrun-{self._dry_run_team_id}"
            logger.info(f"[DRY-RUN] POST {url} payload={json.dumps(payload)} -> fake id {fake_id}")
            return {"id": fake_id, "slug": team_slug or team_name, "name": team_name, "dry_run": True}

        try:
            response = requests.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to create team {team_name}: {str(e)}")
            if hasattr(e.response, 'text'):
                logger.error(f"Response: {e.response.text}")
            raise

    def sync_project_teams(self, export_file_path: str, org_slug: str, source_org: str = None) -> Dict[str, List[str]]:
        """
        Sync team-project relationships from export file. When the export holds multiple orgs,
        `source_org` (a source org slug) selects which one's teams/projects to migrate.
        """
        results = {
            'teams_created': [],
            'teams_failed': [],
            'project_mappings_successful': [],
            'project_mappings_failed': [],
            'team_id_mappings': [],  # New field for storing old PK -> new ID mappings
            'mappings': {}
        }

        try:
            # Load and process export data
            data = self.load_export_data(export_file_path)
            source_pk = self.resolve_source_org_pk(data, source_org)
            if source_pk is not None:
                logger.info(f"Filtering to source org '{source_org or '(only org in file)'}' (pk {source_pk})")
            mappings = self.extract_mappings(data, source_pk=source_pk)
            
            # Convert any sets in mappings to lists for JSON serialization
            for project_data in mappings['projects'].values():
                if 'team_pks' in project_data:
                    project_data['team_pks'] = list(project_data['team_pks'])
            
            results['mappings'] = mappings
            
            # First, create all teams
            for team_pk, team_data in mappings['teams'].items():
                try:
                    team_name = team_data['name']
                    team_slug = team_data['slug']
                    created_team = self.create_team(org_slug, team_name, team_slug)
                    logger.info(f"Created team: {team_slug}")
                    results['teams_created'].append(team_slug)
                    
                    # Store the mapping between old PK and new team ID
                    results['team_id_mappings'].append({
                        'old_pk': team_pk,
                        'new_id': created_team['id'],
                        'slug': team_slug,
                        'name': team_name
                    })
                    
                except Exception as e:
                    logger.error(f"Failed to create team {team_data['name']}: {str(e)}")
                    results['teams_failed'].append(team_data['slug'])
            
            # Then process project-team relationships
            for project_slug, project_data in mappings['projects'].items():
                team_pks = project_data.get('team_pks', [])
                
                for team_pk in team_pks:
                    if team_pk in mappings['teams']:
                        team_slug = mappings['teams'][team_pk]['slug']
                        
                        if self.add_team_to_project(project_slug, team_slug):
                            results['project_mappings_successful'].append(f"{project_slug}:{team_slug}")
                        else:
                            results['project_mappings_failed'].append(f"{project_slug}:{team_slug}")
                    else:
                        logger.warning(f"Team PK {team_pk} not found for project {project_slug}")
                        results['project_mappings_failed'].append(f"{project_slug}:unknown_team_{team_pk}")
            
            return results
            
        except Exception as e:
            logger.error(f"Project-team sync failed: {str(e)}")
            raise

def main():
    import sys
    import argparse

    parser = argparse.ArgumentParser(description='Create Sentry teams and map them to projects')
    parser.add_argument('auth_token', help='Sentry authentication token')
    parser.add_argument('org_slug', help='Destination SaaS organization slug')
    parser.add_argument('export_file', help='JSON export file path')
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

    try:
        dry_run = not args.run_on_real_data
        if dry_run:
            logger.info("=== DRY RUN (default): no changes will be made to SaaS. Pass --run_on_real_data=true to apply. ===")
        else:
            logger.info("=== EXECUTE: changes WILL be made to SaaS ===")
        mapper = SentryTeamProjectMapper(auth_token, org_slug, dry_run=dry_run)
        results = mapper.sync_project_teams(export_file, org_slug, source_org=args.source_org)
        
        logger.info("Project-team sync completed:")
        logger.info(f"Teams created: {len(results['teams_created'])}")
        logger.info(f"Teams failed: {len(results['teams_failed'])}")
        logger.info(f"Project mappings successful: {len(results['project_mappings_successful'])}")
        logger.info(f"Project mappings failed: {len(results['project_mappings_failed'])}")
        
        # Tag output with source org, dest org, and timestamp so per-org runs never overwrite.
        tag = f"{args.source_org or 'allorgs'}_{args.org_slug}_{datetime.now():%Y%m%d_%H%M%S}"
        out = f"project_team_sync_results_{tag}.json"
        with open(out, 'w') as f:
            json.dump(results, f, indent=2)
        logger.info(f"Wrote {out}  (pass this to migrate_alert_rules.py)")

    except Exception as e:
        logger.error(f"Script execution failed: {str(e)}")
        sys.exit(1)


import os as _rl_os, sys as _rl_sys
_rl_sys.path.insert(0, _rl_os.path.join(_rl_os.path.dirname(_rl_os.path.abspath(__file__)), "..", "common"))
_rl_sys.path.insert(0, _rl_os.path.join(_rl_os.path.dirname(_rl_os.path.abspath(__file__)), "common"))
from run_logging import start_run_log


if __name__ == "__main__":
    start_run_log("create_sentry_teams")
    main()
