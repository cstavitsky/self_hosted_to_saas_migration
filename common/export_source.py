"""Read-only parser for a Sentry relocation export file (`export organizations`).

Parses the export JSON (a flat list of `{model, pk, fields}` records) entirely offline -- no network,
no self-hosted token -- and exposes per-project settings. Used by `migrate_project_settings.py`.

Caveat: a relocation export carries `sentry.projectoption` rows for NON-DEFAULT values only, and
does not reliably carry `sentry.organizationoption`. So anything a customer left at its default has
no row, and org-level options aren't available here at all (org-level settings are out of scope --
see DECISIONS.md D9).
"""
import json


def decode_option_value(value):
    """Export `projectoption.value` is stored inconsistently: scalars (int/bool) come through
    natively, but lists/dicts/strings are JSON-encoded strings (e.g. '["*"]', '"1"',
    '"# rules\\n..."'). Decode JSON strings back to their real Python value; leave native values
    and non-JSON strings untouched."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


class ExportSource:
    def __init__(self, export_path: str):
        with open(export_path) as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError("Export must be a JSON list of {model, pk, fields} records.")

        self._orgs = {}          # org pk -> {"slug", "name"}
        self._projects = {}      # project pk -> {"slug", "name", "org_pk"}
        self._options = {}       # project pk -> {option_key: decoded_value}

        for rec in data:
            if not isinstance(rec, dict):
                continue
            model = rec.get("model")
            fields = rec.get("fields") or {}
            pk = rec.get("pk")
            if model == "sentry.organization":
                self._orgs[pk] = {"slug": fields.get("slug"), "name": fields.get("name")}
            elif model == "sentry.project":
                self._projects[pk] = {
                    "slug": fields.get("slug"),
                    "name": fields.get("name"),
                    "org_pk": fields.get("organization"),
                }
            elif model == "sentry.projectoption":
                proj_pk = fields.get("project")
                key = fields.get("key")
                self._options.setdefault(proj_pk, {})[key] = decode_option_value(fields.get("value"))

    def get_projects(self, source_org_slug: str = None) -> list:
        """Return source projects as [{pk, slug, name, org_slug}]. If source_org_slug is given,
        restrict to that org (useful for multi-org export files)."""
        out = []
        for pk, p in self._projects.items():
            org = self._orgs.get(p["org_pk"], {})
            org_slug = org.get("slug")
            if source_org_slug and org_slug != source_org_slug:
                continue
            out.append({"pk": pk, "slug": p["slug"], "name": p["name"], "org_slug": org_slug})
        return out

    def options_for(self, project_pk) -> dict:
        """All decoded projectoption values for a project, keyed by raw option key."""
        return self._options.get(project_pk, {})

    def org_slugs(self) -> list:
        return sorted(o["slug"] for o in self._orgs.values() if o.get("slug"))
