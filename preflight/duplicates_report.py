"""Step 1 of the migration suite: cross-org duplicates / collision report (offline, export-based).

When several self-hosted orgs are consolidated into ONE SaaS org, project names, team slugs, and team
names can collide -- and a team that exists in two orgs may have a *different roster* in each. This tool
reads JSON exports and reports those overlaps BEFORE any migration runs, so they can be
resolved (rename / merge / drop) up front.

Source: JSON exports only (no live instance) -- see DECISIONS.md D7. Each export file may contain
MANY orgs; records are bucketed to their org via the `organization` FK, and every org across every
file is compared against every other in one pool.

What counts as a hard blocker for a merged create (vs. informational):
  - PROJECT collision        -> DANGER. The migration sends each project's existing slug on create, so
    SaaS preserves it; slugs must be unique within a merged SaaS org, so two projects that share a slug
    clash. Detected on the ORIGINAL slug.
  - TEAM SLUG collision      -> DANGER. Teams are created with an explicit slug, which must be unique.
    Collisions where the roster is IDENTICAL across the orgs are skipped (same slug + same people is
    effectively one team, not a merge hazard); only same-slug/different-roster cases are flagged.
  - TEAM NAME collision      -> informational, but flagged with a MEMBERSHIP DIFF (same team name, but a
    different set of people in each org -- a real merge hazard).
  - SIMILAR ORG NAMES        -> informational (helps spot Dor-Org1 / Dor-Org2 / Dor-Org3 families).

Usage:
  python duplicates_report.py export1.json [export2.json ...] [--label PATH=Prefix ...]
      [--similarity 0.6] [--out duplicate_report.json] [--html [duplicate_report.html]]
  (--label prefixes the display name of every org in that file, e.g. Prefix:org-slug.)

Writes duplicate_report.json (and, with --html, a self-contained duplicate_report.html) and exits
non-zero if any HARD collision is found.
"""
import sys
import json
import html as html_lib
import argparse
import logging
from datetime import datetime
from difflib import SequenceMatcher
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def load(path: str):
    with open(path, "r") as f:
        return json.load(f)


def build_orgs(path: str, data: list, label: str = None) -> list:
    """Split ONE export file into its constituent orgs (a file may contain many).

    Records are bucketed to their owning org via the `organization` foreign key (an org pk)
    carried by sentry.team, sentry.project, and sentry.organizationmember. Team rosters are
    resolved within the file: organizationmemberteam(member_pk, team_pk) -> the team's org,
    with the member's email from organizationmember(user_email|email). team_pk and member_pk
    are unique within a file, so the member->team join is unambiguous across orgs.

    Returns a list of per-org models, one per sentry.organization:
      {id, source_file, slug, name, display, teams: {team_pk: {slug,name,members:set}},
       projects: [{slug,name}]}.
    `id` is unique per (file, org_pk) so orgs are never accidentally merged downstream.
    """
    orgs = {}                       # org_pk -> {name, slug}
    teams = {}                      # team_pk -> {org, slug, name, members:set}
    members = {}                    # member_pk -> {org, email}
    projects = defaultdict(list)    # org_pk -> [{slug, name}]
    memberteam = []                 # (member_pk, team_pk)

    for idx, item in enumerate(data):
        if not isinstance(item, dict):
            if item is None:
                logger.warning(f"{path}: skipping null entry at index {idx}")
            else:
                logger.warning(
                    f"{path}: skipping unexpected {type(item).__name__} entry at index {idx}: {item!r:.120}"
                )
            continue
        model = item.get("model")
        pk = item.get("pk")
        f = item.get("fields", {}) or {}
        if model == "sentry.organization":
            orgs[pk] = {"name": f.get("name"), "slug": f.get("slug")}
        elif model == "sentry.team":
            teams[pk] = {"org": f.get("organization"), "slug": f.get("slug"),
                         "name": f.get("name"), "members": set()}
        elif model == "sentry.organizationmember":
            members[pk] = {"org": f.get("organization"),
                           "email": f.get("user_email") or f.get("email")}
        elif model == "sentry.organizationmemberteam":
            memberteam.append((f.get("organizationmember"), f.get("team")))
        elif model == "sentry.project":
            projects[f.get("organization")].append({"slug": f.get("slug"), "name": f.get("name")})

    for member_pk, team_pk in memberteam:
        t = teams.get(team_pk)
        m = members.get(member_pk)
        if t and m and m["email"]:
            t["members"].add(m["email"])

    basename = path.rsplit("/", 1)[-1]
    result = []
    for org_pk, o in orgs.items():
        # Identity precedence: explicit --label prefix, then org slug, then org name, then file#pk.
        base_display = o["slug"] or o["name"] or f"{basename}#{org_pk}"
        display = f"{label}:{base_display}" if label else base_display
        org_teams = {tpk: {"slug": t["slug"], "name": t["name"], "members": t["members"]}
                     for tpk, t in teams.items() if t["org"] == org_pk}
        result.append({
            "id": f"{basename}#{org_pk}",
            "source_file": path,
            "slug": o["slug"] or display,
            "name": o["name"] or display,
            "display": display,
            "teams": org_teams,
            "projects": projects.get(org_pk, []),
        })
    return result


def _group(orgs, extractor):
    """Group by a normalized key across orgs. extractor(org) -> list of (key, payload).
    Returns key -> list of (org_display, payload) with >1 org represented."""
    grouped = defaultdict(list)
    for org in orgs:
        for key, payload in extractor(org):
            if key:
                grouped[key].append((org["display"], payload))
    return {k: v for k, v in grouped.items() if len({d for d, _ in v}) > 1}


def project_collisions(orgs):
    """Group projects across orgs by their ORIGINAL slug. The migration sends each project's existing
    slug on create, so SaaS preserves it; slugs must be unique within a merged org, so two projects
    that share a slug are what actually clash. Every Sentry project has a slug, so no fallback."""
    def extract(o):
        rows = []
        for p in o["projects"]:
            key = _norm(p["slug"])
            rows.append((key, {"slug": p["slug"], "name": p["name"], "key": key}))
        return rows
    return _group(orgs, extract)


def team_collisions_with_membership(orgs, key_field):
    """Group teams across orgs by key_field ('name' or 'slug'); attach a membership diff per group."""
    grouped = defaultdict(dict)  # key -> {org_display: set(members)}
    display_name = {}            # key -> a representative original label
    for org in orgs:
        for t in org["teams"].values():
            key = _norm(t.get(key_field))
            if not key:
                continue
            grouped[key].setdefault(org["display"], set()).update(t["members"])
            display_name.setdefault(key, t.get(key_field))

    collisions = {}
    for key, per_org in grouped.items():
        if len(per_org) < 2:
            continue
        all_sets = list(per_org.values())
        common = set.intersection(*all_sets) if all_sets else set()
        membership = {}
        for org_display, mset in per_org.items():
            others = set().union(*[s for d, s in per_org.items() if d != org_display]) if len(per_org) > 1 else set()
            membership[org_display] = {
                "members": sorted(mset),
                "unique_to_this_org": sorted(mset - others),
            }
        collisions[key] = {
            "label": display_name[key],
            "orgs": sorted(per_org.keys()),
            "common_members": sorted(common),
            "membership": membership,
            "identical_rosters": all(s == all_sets[0] for s in all_sets),
        }
    return collisions


def similar_org_names(orgs, threshold: float):
    pairs = []
    for i in range(len(orgs)):
        for j in range(i + 1, len(orgs)):
            a, b = orgs[i], orgs[j]
            ratio = SequenceMatcher(None, _norm(a["name"]), _norm(b["name"])).ratio()
            if ratio >= threshold:
                pairs.append({"a": a["display"], "b": b["display"], "ratio": round(ratio, 3)})
    return sorted(pairs, key=lambda p: p["ratio"], reverse=True)


def _print_project_section(title, dups, note):
    logger.info(f"\n=== {title} ===")
    if not dups:
        logger.info("  none")
        return
    for key, group in sorted(dups.items()):
        where = ", ".join(f"{org} (name '{p['name']}', slug '{p['slug']}')" for org, p in group)
        logger.info(f"  slug '{key}' in {len(group)} orgs: {where}   [{note}]")


def _print_team_section(title, collisions, note):
    logger.info(f"\n=== {title} ===")
    if not collisions:
        logger.info("  none")
        return
    for key, info in sorted(collisions.items()):
        roster = "identical rosters" if info["identical_rosters"] else "DIFFERENT rosters"
        logger.info(f"  '{info['label']}' in {', '.join(info['orgs'])}  [{note}; {roster}]")
        if info["common_members"]:
            logger.info(f"      common: {', '.join(info['common_members'])}")
        else:
            logger.info("      common: (none)")
        for org_display, m in info["membership"].items():
            uniq = ", ".join(m["unique_to_this_org"]) or "(none)"
            logger.info(f"      {org_display}: members [{', '.join(m['members']) or '(none)'}] | unique [{uniq}]")


def esc(s) -> str:
    return html_lib.escape(str(s), quote=True)


def _html_project_section(title, note_class, dups):
    if not dups:
        return f'<section><h2>{esc(title)}</h2><p class="empty">none</p></section>'
    rows = []
    for key, group in sorted(dups.items()):
        items = []
        for item in group:
            items.append(
                f'<li data-org="{esc(item["org"])}"><span class="org">{esc(item["org"])}</span> '
                f'&mdash; slug <code>{esc(item["slug"])}</code>, name &ldquo;{esc(item["name"])}&rdquo;</li>'
            )
        orgs = "".join(items)
        rows.append(
            f'<tr data-group="project"><td class="key"><code>{esc(key)}</code></td>'
            f'<td><span class="badge {note_class}">{esc(note_class.upper())}</span></td>'
            f'<td><ul class="orglist">{orgs}</ul></td></tr>'
        )
    return (
        f'<section><h2>{esc(title)}</h2>'
        f'<table><thead><tr><th>Slug</th><th>Severity</th><th>Appears in</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></section>'
    )


def _html_team_section(title, note_class, collisions, key_noun="team"):
    if not collisions:
        return f'<section><h2>{esc(title)}</h2><p class="empty">none</p></section>'
    blocks = []
    for key, info in sorted(collisions.items()):
        common = (
            ", ".join(esc(m) for m in info["common_members"]) if info["common_members"] else "(none)"
        )
        per_org = []
        for org_display, m in info["membership"].items():
            members = ", ".join(esc(x) for x in m["members"]) or "(none)"
            unique = ", ".join(esc(x) for x in m["unique_to_this_org"]) or "(none)"
            members_json = esc(json.dumps(m["members"]))
            per_org.append(
                f'<div class="orgroster" data-org="{esc(org_display)}" data-members="{members_json}">'
                f'<div class="org"><span class="mlabel">organization:</span> {esc(org_display)}</div>'
                f'<div class="mline"><span class="mlabel">members:</span> {members}</div>'
                f'<div class="mline"><span class="mlabel">unique to this org:</span> '
                f'<span class="unique">{unique}</span></div></div>'
            )
        blocks.append(
            f'<div class="teamblock">'
            f'<div class="teamhead"><span class="keylabel">duplicate {esc(key_noun)}:</span> '
            f'<code class="key">{esc(info["label"])}</code> '
            f'<span class="badge {note_class}">{esc(note_class.upper())}</span> '
            f'<span class="badge different roster-badge">{"DIFFERENT rosters" if not info["identical_rosters"] else "identical rosters"}</span> '
            f'<span class="inorgs">in {esc(", ".join(info["orgs"]))}</span></div>'
            f'<div class="common"><span class="mlabel">common members:</span> <span class="common-val">{common}</span></div>'
            f'<div class="rosters">{"".join(per_org)}</div></div>'
        )
    return f'<section><h2>{esc(title)}</h2>{"".join(blocks)}</section>'


def render_html(report: dict, exports: list, generated_at: str) -> str:
    s = report["summary"]
    hard = s["hard_collisions"]
    hard_class = "danger" if hard else "ok"

    org_cards = "".join(
        f'<div class="card" data-org="{esc(o["display"])}"><div class="cardname">{esc(o["display"])}</div>'
        f'<div class="cardsub">{esc(o["name"])}</div>'
        f'<div class="cardstats">{o["teams"]} teams &middot; {o["projects"]} projects</div>'
        f'<div class="cardfile"><code>{esc(o["source_file"])}</code></div></div>'
        for o in report["orgs"]
    )

    files = ", ".join(esc(f) for f in exports)

    # Plain (non-f) string so JS braces don't need escaping. Recomputes the view when orgs are
    # toggled: a collision row stays visible only if >=2 still-selected orgs share it, and team
    # rosters/common/unique members are recomputed among the selected orgs.
    script = """<script>
(function () {
  var deselected = new Set();

  function recompute() {
    // Project rows: keep row only if >=2 selected orgs still share the slug.
    document.querySelectorAll('tr[data-group="project"]').forEach(function (tr) {
      var visible = 0;
      tr.querySelectorAll('li[data-org]').forEach(function (li) {
        var off = deselected.has(li.getAttribute('data-org'));
        li.style.display = off ? 'none' : '';
        if (!off) visible++;
      });
      tr.style.display = visible >= 2 ? '' : 'none';
    });

    // Team blocks: recompute rosters, common, unique among selected orgs.
    document.querySelectorAll('.teamblock').forEach(function (tb) {
      var rosters = [].slice.call(tb.querySelectorAll('.orgroster[data-org]'));
      var shown = [];
      rosters.forEach(function (r) {
        var off = deselected.has(r.getAttribute('data-org'));
        r.style.display = off ? 'none' : '';
        if (!off) shown.push(r);
      });
      var names = shown.map(function (r) { return r.getAttribute('data-org'); });
      var sets = shown.map(function (r) {
        try { return JSON.parse(r.getAttribute('data-members') || '[]'); } catch (e) { return []; }
      });
      var common = sets.length
        ? sets[0].filter(function (m) { return sets.every(function (s) { return s.indexOf(m) !== -1; }); })
        : [];
      shown.forEach(function (r, i) {
        var others = sets.filter(function (_, j) { return j !== i; });
        var uniq = sets[i].filter(function (m) {
          return !others.some(function (s) { return s.indexOf(m) !== -1; });
        });
        var uEl = r.querySelector('.unique');
        if (uEl) uEl.textContent = uniq.length ? uniq.join(', ') : '(none)';
      });
      var cEl = tb.querySelector('.common-val');
      if (cEl) cEl.textContent = common.length ? common.join(', ') : '(none)';
      var inEl = tb.querySelector('.inorgs');
      if (inEl) inEl.textContent = names.length ? ('in ' + names.join(', ')) : '';
      var identical = sets.length >= 2 && sets.every(function (s) {
        return s.length === sets[0].length && s.every(function (m) { return sets[0].indexOf(m) !== -1; });
      });
      var b = tb.querySelector('.roster-badge');
      if (b) b.textContent = identical ? 'identical rosters' : 'DIFFERENT rosters';
      // Show only when >=2 selected orgs share the team AND their rosters differ (identical
      // rosters are treated as the same team and skipped).
      tb.style.display = (names.length >= 2 && !identical) ? '' : 'none';
    });

    updateCounts();
  }

  function visibleCount(sel) {
    return [].filter.call(document.querySelectorAll(sel), function (el) {
      return el.style.display !== 'none';
    }).length;
  }

  function setText(id, v) { var el = document.getElementById(id); if (el) el.textContent = v; }

  function updateCounts() {
    var proj = visibleCount('tr[data-group="project"]');
    var team = visibleCount('.teamblock');
    var hard = proj + team;
    setText('count-project', proj);
    setText('count-teamslug', team);
    setText('count-hard', hard);
    setText('bignum', hard);
    var big = document.getElementById('bignum');
    if (big) { big.classList.toggle('danger', hard > 0); big.classList.toggle('ok', hard === 0); }
    var cards = document.querySelectorAll('.card[data-org]');
    setText('org-status', (cards.length - deselected.size) + ' of ' + cards.length + ' orgs shown');
  }

  document.querySelectorAll('.card[data-org]').forEach(function (card) {
    function toggle() {
      var org = card.getAttribute('data-org');
      if (deselected.has(org)) { deselected.delete(org); card.classList.remove('deselected'); card.setAttribute('aria-pressed', 'true'); }
      else { deselected.add(org); card.classList.add('deselected'); card.setAttribute('aria-pressed', 'false'); }
      recompute();
    }
    card.setAttribute('role', 'button');
    card.setAttribute('tabindex', '0');
    card.setAttribute('aria-pressed', 'true');
    card.addEventListener('click', toggle);
    card.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
    });
  });

  recompute();
})();
</script>"""

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Duplicate / collision report</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    max-width: 980px; margin: 0 auto; padding: 24px; line-height: 1.5; color: #1a1a1a; background: #fff; }}
  h1 {{ margin: 0 0 4px; font-size: 24px; }}
  .meta {{ color: #666; font-size: 13px; margin-bottom: 20px; }}
  .meta code {{ font-size: 12px; }}
  .summary {{ display: flex; gap: 16px; flex-wrap: wrap; align-items: center;
    border: 1px solid #e2e2e2; border-radius: 10px; padding: 16px; margin-bottom: 24px; }}
  .bignum {{ font-size: 40px; font-weight: 700; line-height: 1; padding: 6px 16px; border-radius: 8px; }}
  .bignum.danger {{ background: #fdecea; color: #b3261e; }}
  .bignum.ok {{ background: #e8f5e9; color: #1e7e34; }}
  .counts {{ font-size: 14px; color: #333; }}
  .counts b {{ font-size: 16px; }}
  .legend {{ border: 1px solid #e2e2e2; border-radius: 10px; padding: 12px 16px; margin-bottom: 24px; }}
  .legtitle {{ font-weight: 600; font-size: 13px; text-transform: uppercase; letter-spacing: .04em;
    color: #666; margin-bottom: 8px; }}
  .legitem {{ font-size: 13px; margin-bottom: 6px; }}
  .legitem .badge {{ margin-right: 8px; vertical-align: middle; }}
  .orgcontrols {{ font-size: 13px; color: #666; margin-bottom: 8px; }}
  .orgcontrols b {{ color: #1a1a1a; }}
  .cards {{ display: flex; gap: 12px; flex-wrap: wrap; margin-bottom: 24px; }}
  .card {{ border: 1px solid #e2e2e2; border-radius: 8px; padding: 12px 14px; min-width: 160px;
    cursor: pointer; user-select: none; position: relative; transition: opacity .12s, border-color .12s; }}
  .card:hover {{ border-color: #999; }}
  .card::before {{ content: "\\2713"; position: absolute; top: 8px; right: 10px; font-size: 12px;
    font-weight: 700; color: #1e7e34; }}
  .card.deselected {{ opacity: .45; }}
  .card.deselected::before {{ content: "\\2715"; color: #b3261e; }}
  .card.deselected .cardname {{ text-decoration: line-through; }}
  .cardname {{ font-weight: 600; }}
  .cardsub {{ color: #666; font-size: 13px; }}
  .cardstats {{ margin-top: 6px; font-size: 13px; }}
  .cardfile {{ margin-top: 4px; font-size: 11px; color: #888; }}
  section {{ margin-bottom: 28px; }}
  h2 {{ font-size: 17px; border-bottom: 2px solid #eee; padding-bottom: 6px; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid #eee; vertical-align: top; }}
  th {{ color: #555; font-weight: 600; background: #fafafa; }}
  code {{ background: #f2f2f2; padding: 1px 5px; border-radius: 4px; font-size: 12px; }}
  .key {{ font-weight: 600; }}
  .orglist {{ margin: 0; padding-left: 18px; }}
  .note {{ color: #888; font-size: 12px; }}
  .badge {{ display: inline-block; font-size: 11px; font-weight: 700; padding: 2px 7px; border-radius: 999px;
    letter-spacing: .02em; }}
  .badge.danger {{ background: #fdecea; color: #b3261e; }}
  .badge.info {{ background: #fff4e5; color: #8a5a00; }}
  .badge.different, .badge.same {{ background: #ececec; color: #444; }}
  .empty {{ color: #1e7e34; font-style: italic; }}
  .teamblock {{ border: 1px solid #e2e2e2; border-radius: 8px; padding: 12px 14px; margin-bottom: 12px; }}
  .teamhead {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 6px; }}
  .inorgs {{ color: #666; font-size: 13px; }}
  .common {{ font-size: 13px; margin-bottom: 8px; }}
  .rosters {{ display: flex; gap: 12px; flex-wrap: wrap; }}
  .orgroster {{ background: #fafafa; border: 1px solid #eee; border-radius: 6px; padding: 8px 10px;
    font-size: 13px; min-width: 220px; flex: 1; }}
  .orgroster .org {{ font-weight: 600; margin-bottom: 4px; }}
  .keylabel {{ color: #666; font-size: 13px; }}
  .mlabel {{ color: #666; }}
  .unique {{ color: #b3261e; }}
  @media (prefers-color-scheme: dark) {{
    body {{ color: #e6e6e6; background: #141414; }}
    .summary, .card, .teamblock, .legend {{ border-color: #333; }}
    .legtitle, .legitem {{ color: #9a9a9a; }}
    th {{ background: #1e1e1e; color: #bbb; }}
    th, td {{ border-color: #2a2a2a; }}
    code {{ background: #262626; }}
    .orgroster {{ background: #1c1c1c; border-color: #2a2a2a; }}
    h2 {{ border-color: #2a2a2a; }}
    .cardsub, .cardfile, .inorgs, .mlabel, .counts {{ color: #9a9a9a; }}
    .unique {{ color: #ff6b5e; }}
    .badge.different, .badge.same {{ background: #2e2e2e; color: #cfcfcf; }}
  }}
</style></head>
<body>
  <h1>Duplicate / collision report</h1>
  <div class="meta">Generated {esc(generated_at)} &middot; sources: <code>{files}</code></div>

  <div class="summary">
    <div class="bignum {hard_class}" id="bignum">{hard}</div>
    <div class="counts">
      <div><b id="count-hard">{hard}</b> Danger collision group(s) &mdash; block a merged migration</div>
      <div>project <span id="count-project">{s["project_collisions"]}</span> &middot;
        team-slug <span id="count-teamslug">{s["team_slug_collisions"]}</span>
        &middot; team-name {s["team_name_collisions"]} (info) &middot;
        similar-names {s["similar_org_name_pairs"]} (info)</div>
    </div>
  </div>

  <div class="legend">
    <div class="legtitle">Severity reference</div>
    <div class="legitem"><span class="badge danger">DANGER</span>Will break a merged migration: the
      create fails or silently merges into the wrong object. Resolve (rename / merge / drop) before
      migrating. Any Danger group makes the tool exit non-zero. Covers project collisions (projects
      that share a slug) and team-slug collisions.</div>
  </div>

  <div class="orgcontrols">Click an org to include or exclude it from the analysis below.
    <b id="org-status"></b></div>
  <div class="cards">{org_cards}</div>

  {_html_project_section("Project collisions (Danger - projects share a slug)", "danger", report["project_collisions_HARD"])}
  {_html_team_section("Team slug collisions (Danger - slug must be unique)", "danger", report["team_slug_collisions_HARD"], "team slug")}
{script}
</body></html>
"""


def main():
    parser = argparse.ArgumentParser(description="Cross-org duplicates/collision report from JSON exports")
    parser.add_argument("exports", nargs="+", help="One or more export.json files (each may hold many orgs)")
    parser.add_argument("--label", action="append", default=[], metavar="PATH=PREFIX",
                        help="Prefix the display name of every org from a given export path (repeatable)")
    parser.add_argument("--similarity", type=float, default=0.6,
                        help="Org-name similarity threshold 0..1 for the 'similar names' section (default 0.6)")
    parser.add_argument("--out", default="duplicate_report.json", help="Output JSON path")
    parser.add_argument("--html", nargs="?", const="duplicate_report.html", default=None, metavar="PATH",
                        help="Also write a self-contained HTML report (default duplicate_report.html)")
    args = parser.parse_args()

    labels = {}
    for pair in args.label:
        if "=" in pair:
            path, name = pair.split("=", 1)
            labels[path] = name

    orgs = []
    for path in args.exports:
        file_orgs = build_orgs(path, load(path), label=labels.get(path))
        orgs.extend(file_orgs)
        summary = ", ".join(f"{o['display']} ({len(o['teams'])}t/{len(o['projects'])}p)" for o in file_orgs)
        logger.info(f"Loaded {path}: {len(file_orgs)} org(s) -> {summary}")

    # Displays are the dedup key in the collision grouping, so they must be unique across the
    # pool -- otherwise two orgs that happen to share a slug would be collapsed and their
    # clashes hidden. Disambiguate any duplicate display with its source file + org pk.
    by_display = defaultdict(list)
    for o in orgs:
        by_display[o["display"]].append(o)
    for disp, group in by_display.items():
        if len(group) > 1:
            for o in group:
                o["display"] = f"{disp} [{o['id']}]"

    proj_dups = project_collisions(orgs)
    team_slug_dups = team_collisions_with_membership(orgs, "slug")
    # Skip team-slug collisions whose rosters are identical across the orgs: same slug + same people
    # is effectively the same team, not a merge hazard worth flagging.
    team_slug_dups = {k: v for k, v in team_slug_dups.items() if not v["identical_rosters"]}
    team_name_dups = team_collisions_with_membership(orgs, "name")
    similar = similar_org_names(orgs, args.similarity)

    _print_project_section("PROJECT collisions (DANGER - projects share a slug)", proj_dups, "DANGER")
    _print_team_section("TEAM SLUG collisions (DANGER - slug must be unique)", team_slug_dups, "DANGER")
    _print_team_section("TEAM NAME collisions (INFO - watch roster diffs)", team_name_dups, "info")

    logger.info("\n=== SIMILAR ORG NAMES (informational) ===")
    if similar:
        for p in similar:
            logger.info(f"  '{p['a']}' ~ '{p['b']}'  (ratio {p['ratio']})")
    else:
        logger.info("  none above threshold")

    def _proj_json(dups):
        return {k: [{"org": o, "slug": p["slug"], "name": p["name"], "match_key": p["key"]}
                    for o, p in g] for k, g in dups.items()}

    report = {
        "orgs": [{"display": o["display"], "slug": o["slug"], "name": o["name"],
                  "source_file": o["source_file"], "teams": len(o["teams"]),
                  "projects": len(o["projects"])} for o in orgs],
        "project_collisions_HARD": _proj_json(proj_dups),
        "team_slug_collisions_HARD": team_slug_dups,
        "team_name_collisions_info": team_name_dups,
        "similar_org_names": similar,
    }
    hard = len(proj_dups) + len(team_slug_dups)
    report["summary"] = {
        "orgs": len(orgs),
        "hard_collisions": hard,
        "project_collisions": len(proj_dups),
        "team_slug_collisions": len(team_slug_dups),
        "team_name_collisions": len(team_name_dups),
        "similar_org_name_pairs": len(similar),
    }

    with open(args.out, "w") as f:
        json.dump(report, f, indent=2)

    if args.html:
        with open(args.html, "w") as f:
            f.write(render_html(report, args.exports, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    logger.info("\n" + "=" * 64)
    logger.info(f"Summary: {len(orgs)} orgs | Danger collisions: {hard} "
                f"(project {len(proj_dups)}, team-slug {len(team_slug_dups)}) | "
                f"info: team-name {len(team_name_dups)}, similar-names {len(similar)}")
    logger.info(f"Wrote {args.out}")
    if args.html:
        logger.info(f"Wrote {args.html}")

    if hard:
        logger.info(f"\nFOUND {hard} Danger collision group(s) that will break a merged migration. "
                    f"Resolve (rename/merge/drop) before migrating.")
        sys.exit(2)
    logger.info("\nNo hard collisions detected.")


import os as _rl_os, sys as _rl_sys
_rl_sys.path.insert(0, _rl_os.path.join(_rl_os.path.dirname(_rl_os.path.abspath(__file__)), "..", "common"))
_rl_sys.path.insert(0, _rl_os.path.join(_rl_os.path.dirname(_rl_os.path.abspath(__file__)), "common"))
from run_logging import start_run_log


if __name__ == "__main__":
    start_run_log("duplicates_report")
    main()
