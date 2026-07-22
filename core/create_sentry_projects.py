import json
import requests
import logging
from typing import Dict, List
from collections import defaultdict
from datetime import datetime

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


#requires team to already exist, always uses 'migration' as the team.
class SentryProjectManager:
    def __init__(self, auth_token: str, base_url: str = "https://sentry.io/api/0", dry_run: bool = False):
        self.base_url = base_url
        self.dry_run = dry_run
        self.headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json"
        }

    def load_data_from_json(self, json_file_path: str) -> List[Dict]:
        """
        Load data from a JSON file
        """
        try:
            with open(json_file_path, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON file: {str(e)}")
            raise
        except FileNotFoundError:
            logger.error(f"File not found: {json_file_path}")
            raise

    def delete_project(self, org_slug: str, project_slug: str) -> bool:
        """
        Delete a project from the organization
        """
        url = f"{self.base_url}/projects/{org_slug}/{project_slug}/"

        if self.dry_run:
            logger.info(f"[DRY-RUN] DELETE {url}")
            return True

        try:
            response = requests.delete(url, headers=self.headers)
            response.raise_for_status()
            logger.info(f"Successfully scheduled deletion for project: {project_slug}")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to delete project {project_slug}: {str(e)}")
            if hasattr(e.response, 'text'):
                logger.error(f"Response: {e.response.text}")
            return False

    def create_project(self, org_slug: str, project_config: Dict) -> Dict:
        """
        Create a new Sentry project for the specified organization.
        Always uses 'migration' as the team.
        """
        url = f"{self.base_url}/teams/{org_slug}/migration/projects/"

        # Send name AND the original slug so SaaS preserves the source slug instead of deriving
        # one from the name. The Sentry API requires 'name'; when 'slug' is also supplied it is
        # used as-is (validated for format/uniqueness) rather than re-derived.
        payload = {
            "name": project_config['name'],
            "slug": project_config['slug'],
            "platform": project_config['platform']
        }

        if self.dry_run:
            logger.info(f"[DRY-RUN] POST {url} payload={json.dumps(payload)} -> requested slug '{project_config['slug']}'")
            return {"slug": project_config['slug'], "name": project_config['name'], "dry_run": True}

        try:
            response = requests.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to create project: {str(e)}")
            if hasattr(e.response, 'text'):
                logger.error(f"Response: {e.response.text}")
            raise

    @staticmethod
    def resolve_source_org_pk(data: List[Dict], source_org: str = None):
        """Resolve which SOURCE org's records to migrate. An export may contain many orgs; each
        record carries an `organization` FK (the source org pk). Returns that pk, or None to mean
        'no filter' (only when the file holds a single org and no --source-org was given).

        - source_org given: match it to a sentry.organization slug and return its pk.
        - source_org omitted + one org in file: return that org's pk.
        - source_org omitted + multiple orgs: refuse to guess.
        """
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

    def sync_projects(self, json_file_path: str, org_slug: str, delete_mode: bool = False,
                      source_org: str = None) -> Dict[str, List[str]]:
        """
        Create or delete projects from JSON file based on mode. When the export holds multiple orgs,
        `source_org` (a source org slug) selects which one's projects to migrate into `org_slug`.
        """
        results = {
            'created': [],
            'failed': [],
            'deleted': [],
            'delete_failed': []
        }

        try:
            data = self.load_data_from_json(json_file_path)
            source_pk = self.resolve_source_org_pk(data, source_org)
            if source_pk is not None:
                logger.info(f"Filtering to source org '{source_org or '(only org in file)'}' (pk {source_pk})")

            for item in data:
                if not isinstance(item, dict):
                    continue
                if item.get('model') == 'sentry.project':
                    fields = item.get('fields', {}) or {}
                    if source_pk is not None and fields.get('organization') != source_pk:
                        continue
                    project_slug = fields.get('slug')

                    if delete_mode:
                        if self.delete_project(org_slug, project_slug):
                            results['deleted'].append(project_slug)
                        else:
                            results['delete_failed'].append(project_slug)
                    else:
                        project_config = {
                            'name': fields.get('name'),
                            'slug': project_slug,
                            'platform': fields.get('platform') or 'python',
                        }

                        try:
                            created_project = self.create_project(org_slug, project_config)
                            logger.info(f"Created project: {created_project['slug']}")
                            results['created'].append(created_project['slug'])
                        except Exception as e:
                            logger.error(f"Failed to create project {project_slug}: {str(e)}")
                            results['failed'].append(project_slug)

            return results

        except Exception as e:
            logger.error(f"Project sync failed: {str(e)}")
            raise

def main():
    import sys
    import argparse

    parser = argparse.ArgumentParser(description='Manage Sentry projects')
    parser.add_argument('auth_token', help='Sentry authentication token')
    parser.add_argument('org_slug', help='Destination SaaS organization slug')
    parser.add_argument('export_file', help='JSON export file path')
    parser.add_argument('--source-org', help='Source org slug to migrate (required when the export holds multiple orgs)')
    parser.add_argument('--delete', action='store_true', help='Delete projects instead of creating them')
    parser.add_argument('--run_on_real_data', type=lambda v: str(v).strip().lower() in ('true', '1', 'yes', 'y'),
                        default=False, metavar='true|false',
                        help='Set to true to actually perform changes. Default false = dry-run.')
    parser.add_argument('--dry-run', action='store_true',
                        help='(default) Dry-run is on by default; accepted for compatibility and is a no-op.')

    args = parser.parse_args()

    try:
        dry_run = not args.run_on_real_data
        manager = SentryProjectManager(args.auth_token, dry_run=dry_run)
        if dry_run:
            logger.info("=== DRY RUN (default): no changes will be made to SaaS. Pass --run_on_real_data=true to apply. ===")
        else:
            logger.info("=== EXECUTE: changes WILL be made to SaaS ===")
        results = manager.sync_projects(args.export_file, args.org_slug, args.delete, source_org=args.source_org)

        logger.info("Project management completed:")
        if args.delete:
            logger.info(f"Deleted projects: {len(results['deleted'])}")
            logger.info(f"Failed deletions: {len(results['delete_failed'])}")
        else:
            logger.info(f"Created projects: {len(results['created'])}")
            logger.info(f"Failed creations: {len(results['failed'])}")

        # Tag output with source org, dest org, and timestamp so per-org runs never overwrite.
        tag = f"{args.source_org or 'allorgs'}_{args.org_slug}_{datetime.now():%Y%m%d_%H%M%S}"
        out = f"project_management_results_{tag}.json"
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
    start_run_log("create_sentry_projects")
    main()
