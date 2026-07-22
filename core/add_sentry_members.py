import json
import requests
import logging
from typing import Dict, List
from collections import defaultdict
import argparse
from datetime import datetime
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

#does not send invite emails. integration tokens are restricted to invting member roles only
class SentryMemberManager:
    def __init__(self, auth_token: str, test_email: str = None, base_url: str = "https://sentry.io/api/0", dry_run: bool = False, send_invite: bool = False):
        self.base_url = base_url
        self.dry_run = dry_run
        self.send_invite = send_invite
        self.headers = {
            "Authorization": f"Bearer {auth_token}",
            "Content-Type": "application/json"
        }
        self.test_email = test_email
        self._dry_run_member_id = 0

    def generate_test_email(self, original_email: str, internal_id: str) -> str:
        """
        Generate a unique test email using the Gmail + modifier
        """
        if not self.test_email:
            return original_email
            
        timestamp = datetime.now().strftime("%Y%m%d%H%M")
        base, domain = self.test_email.split('@')
        return f"{base}+test{internal_id}_{timestamp}@{domain}"

    def load_export_data(self, export_file_path: str) -> List[Dict]:
        """
        Load and parse the JSON export file
        """
        try:
            with open(export_file_path, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load export file {export_file_path}: {str(e)}")
            raise

    @staticmethod
    def resolve_source_org_pk(data: List[Dict], source_org: str = None):
        """Resolve which SOURCE org's records to migrate. An export may contain many orgs;
        organizationmember records carry an `organization` FK (the source org pk). Returns that pk,
        or None (no filter) only when the file holds a single org and no --source-org was given."""
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

    def sync_members(self, export_file_path: str, org_slug: str, source_org: str = None,
                     team_mappings_out: str = 'user_mappings_for_teams.json') -> Dict[str, Dict]:
        """
        Add active users as members and track ID mappings
        """
        results = {
            'id_mappings': {
                'success': {},  # {internal_id: {'sentry_id': id, 'email': email, 'original_email': original}}
                'failed': {}    # {internal_id: {'error': error_msg, 'email': email, 'original_email': original}}
            },
            'stats': {
                'added': 0,
                'failed': 0,
                'skipped': 0
            }
        }
        
        # Also create a separate export file for team assignments
        team_assignment_mappings = {
            'user_mappings': {}  # {original_pk: sentry_member_id}
        }
        
        try:
            data = self.load_export_data(export_file_path)
            source_pk = self.resolve_source_org_pk(data, source_org)
            if source_pk is not None:
                logger.info(f"Filtering to source org '{source_org or '(only org in file)'}' (pk {source_pk})")

            for item in data:
                if not isinstance(item, dict):
                    continue
                if item.get('model') == 'sentry.organizationmember':
                    fields = item.get('fields', {}) or {}
                    if source_pk is not None and fields.get('organization') != source_pk:
                        continue
                    internal_id = str(item.get('pk'))

                    # Only process active users
                    if not fields.get('user_is_active', False):
                        results['stats']['skipped'] += 1
                        continue

                    original_email = fields.get('user_email')
                    # Members with no email can't be created (Sentry requires an email); skip with a
                    # clear reason rather than letting the API reject them as a hard failure. This is
                    # typically a never-accepted invite or a service/provisioned account.
                    if not original_email:
                        logger.warning(f"  SKIP: organizationmember pk {internal_id}: email is null in "
                                       f"the export (likely a never-accepted invite or service account) "
                                       f"-- cannot create member")
                        results['stats']['skipped'] += 1
                        results['id_mappings'].setdefault('skipped_no_email', {})[internal_id] = {
                            'reason': 'email is null in export'
                        }
                        continue

                    email = self.generate_test_email(original_email, internal_id)
                    role = fields.get('role', 'member')
                    
                    # Set up team roles if user is admin
                    team_roles = None
                    if role == 'admin':
                        team_roles = ["admin"]
                    
                    try:
                        created_member = self.add_member(
                            org_slug=org_slug,
                            email=email,
                            org_role=role,
                            team_roles=team_roles
                        )
                        
                        sentry_member_id = created_member.get('id')
                        
                        # Store successful mapping
                        results['id_mappings']['success'][internal_id] = {
                            'sentry_id': sentry_member_id,
                            'email': email,
                            'original_email': original_email
                        }
                        
                        # Add to team assignment mappings
                        team_assignment_mappings['user_mappings'][internal_id] = sentry_member_id
                        
                        results['stats']['added'] += 1
                        logger.info(f"Added member: {email} (original: {original_email}) with role {role}")
                        
                    except Exception as e:
                        # Store failed mapping
                        error_msg = str(e)
                        if hasattr(e, 'response') and hasattr(e.response, 'text'):
                            error_msg = e.response.text
                        
                        results['id_mappings']['failed'][internal_id] = {
                            'error': error_msg,
                            'email': email,
                            'original_email': original_email
                        }
                        results['stats']['failed'] += 1
                        logger.error(f"Failed to add member {email} (original: {original_email}): {error_msg}")
            
            # Write the team assignment mappings to a separate file
            with open(team_mappings_out, 'w') as f:
                json.dump(team_assignment_mappings, f, indent=2)
            logger.info(f"Wrote team-assignment mappings: {team_mappings_out}")

            return results
            
        except Exception as e:
            logger.error(f"Member sync failed: {str(e)}")
            raise

    def delete_member(self, org_slug: str, member_id: str) -> bool:
        """
        Delete a member from the organization
        """
        url = f"{self.base_url}/organizations/{org_slug}/members/{member_id}/"

        if self.dry_run:
            logger.info(f"[DRY-RUN] DELETE {url}")
            return True

        try:
            response = requests.delete(url, headers=self.headers)
            response.raise_for_status()
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to delete member {member_id}: {str(e)}")
            if hasattr(e.response, 'text'):
                logger.error(f"Response: {e.response.text}")
            return False

    def delete_members(self, mappings_file: str, org_slug: str) -> Dict[str, List[str]]:
        """
        Delete members using the mappings file
        """
        results = {
            'deleted': [],
            'failed': []
        }
        
        try:
            # Load the mappings file
            with open(mappings_file, 'r') as f:
                mappings = json.load(f)
            
            # Process successful members
            for internal_id, member_data in mappings['id_mappings']['success'].items():
                sentry_id = member_data['sentry_id']
                email = member_data['email']
                
                if self.delete_member(org_slug, sentry_id):
                    results['deleted'].append({
                        'internal_id': internal_id,
                        'sentry_id': sentry_id,
                        'email': email
                    })
                    logger.info(f"Deleted member: {email} (Sentry ID: {sentry_id})")
                else:
                    results['failed'].append({
                        'internal_id': internal_id,
                        'sentry_id': sentry_id,
                        'email': email
                    })
                    logger.error(f"Failed to delete member: {email} (Sentry ID: {sentry_id})")
            
            return results
            
        except Exception as e:
            logger.error(f"Member deletion failed: {str(e)}")
            raise

    def add_member(self, org_slug: str, email: str, org_role: str, team_roles: List[Dict] = None) -> Dict:
        """
        Add a member to the organization
        """
        url = f"{self.base_url}/organizations/{org_slug}/members/"
        
        payload = {
            "email": email,
            "orgRole": "member",# restricted to member role via integration token
            "sendInvite": self.send_invite,  # controlled by --send-invite
            "reinvite": self.send_invite # controlled by --send-invite
        }
        
        # if team_roles:
        #     payload["teamRoles"] = team_roles
        #     del payload["orgRole"]

        if self.dry_run:
            self._dry_run_member_id += 1
            fake_id = f"dryrun-{self._dry_run_member_id}"
            logger.info(f"[DRY-RUN] POST {url} payload={json.dumps(payload)} -> fake id {fake_id}")
            return {"id": fake_id, "email": email, "dry_run": True}

        try:
            response = requests.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to add member {email}: {str(e)}")
            if hasattr(e.response, 'text'):
                logger.error(f"Response: {e.response.text}")
            raise

def main():
    parser = argparse.ArgumentParser(description='Add or delete members from Sentry organization')
    parser.add_argument('auth_token', help='Sentry authentication token')
    parser.add_argument('org_slug', help='Destination SaaS organization slug')
    parser.add_argument('export_file', nargs='?', help='JSON export file path (required unless --delete is used)')
    parser.add_argument('--source-org', help='Source org slug to migrate (required when the export holds multiple orgs)')
    parser.add_argument('--delete', help='Delete members using mappings file', metavar='MAPPINGS_FILE')
    parser.add_argument('--test', help='Test mode with Gmail alias (e.g., your.email@gmail.com)', metavar='EMAIL')
    parser.add_argument('--run_on_real_data', type=lambda v: str(v).strip().lower() in ('true', '1', 'yes', 'y'),
                        default=False, metavar='true|false',
                        help='Set to true to actually perform changes. Default false = dry-run.')
    parser.add_argument('--dry-run', action='store_true',
                        help='(default) Dry-run is on by default; accepted for compatibility and is a no-op.')
    parser.add_argument('--send-invite', action='store_true', help='Send invitation emails (sets sendInvite/reinvite true)')

    args = parser.parse_args()

    try:
        dry_run = not args.run_on_real_data
        if dry_run:
            logger.info("=== DRY RUN (default): no changes will be made to SaaS. Pass --run_on_real_data=true to apply. ===")
        else:
            logger.info("=== EXECUTE: changes WILL be made to SaaS ===")
        if args.send_invite:
            logger.info("=== send-invite ON: invitation emails will be sent ===")
        manager = SentryMemberManager(args.auth_token, test_email=args.test, dry_run=dry_run, send_invite=args.send_invite)

        # Tag outputs with source org, dest org, and timestamp so per-org runs never overwrite.
        tag = f"{args.source_org or 'allorgs'}_{args.org_slug}_{datetime.now():%Y%m%d_%H%M%S}"

        if args.delete:
            # Delete mode
            results = manager.delete_members(args.delete, args.org_slug)

            logger.info("Member deletion completed:")
            logger.info(f"Deleted members: {len(results['deleted'])}")
            logger.info(f"Failed deletions: {len(results['failed'])}")

            # Write deletion results to file
            out = f"member_deletion_results_{tag}.json"
            with open(out, 'w') as f:
                json.dump(results, f, indent=2)
            logger.info(f"Wrote {out}")

        elif args.export_file:
            # Add mode
            team_mappings_out = f"user_mappings_for_teams_{tag}.json"
            results = manager.sync_members(args.export_file, args.org_slug, source_org=args.source_org,
                                           team_mappings_out=team_mappings_out)

            logger.info("Member sync completed:")
            logger.info(f"Added members: {results['stats']['added']}")
            logger.info(f"Failed additions: {results['stats']['failed']}")
            no_email = len(results['id_mappings'].get('skipped_no_email', {}))
            logger.info(f"Skipped (inactive or no email): {results['stats']['skipped']}"
                        + (f" (of which {no_email} had no email)" if no_email else ""))

            # Write results to file
            out = f"member_id_mappings_{tag}.json"
            with open(out, 'w') as f:
                json.dump(results, f, indent=2)
            logger.info(f"Wrote {out}  (pass {team_mappings_out} to assign_team_members.py)")
        
        else:
            parser.error("Provide a positional export_file to add members, or --delete MAPPINGS_FILE to delete")
            
    except Exception as e:
        logger.error(f"Script execution failed: {str(e)}")
        sys.exit(1)


import os as _rl_os, sys as _rl_sys
_rl_sys.path.insert(0, _rl_os.path.join(_rl_os.path.dirname(_rl_os.path.abspath(__file__)), "..", "common"))
_rl_sys.path.insert(0, _rl_os.path.join(_rl_os.path.dirname(_rl_os.path.abspath(__file__)), "common"))
from run_logging import start_run_log


if __name__ == "__main__":
    start_run_log("add_sentry_members")
    main()
