#!/usr/bin/env python3
"""Strip user/member records from a Sentry export (relocation) JSON file.

A Sentry export is a JSON array of records, each shaped like:
    {"model": "sentry.user", "pk": 1, "fields": {...}}

This script drops every record whose "model" is in the removal set
(by default: sentry.user and sentry.organizationmember) and writes the
remainder to a new file with "_minus_members" inserted before ".json".

Usage:
    python strip_members.py export.json
    python strip_members.py export.json --models sentry.user sentry.organizationmember sentry.useremail
    python strip_members.py export.json -o /path/to/output.json
    # Domain scrub: keep the local part, rewrite only the domain (alice@corp.com -> alice@example.com)
    python strip_members.py export.json --email_domain_to_scrub=corp.com
    python strip_members.py export.json --email_domain_to_scrub=corp.com --replacement-domain acme.test
"""

import argparse
import json
import os
import re
import sys

# Models removed unless overridden with --models.
# Grouped by why they're removed; all hold or embed email addresses.
DEFAULT_MODELS = [
    # Email *is* the record.
    "sentry.user",                    # email, email_unique, username
    "sentry.useremail",               # email
    "sentry.email",                   # email
    # "sentry.organizationmember",      # email (invite) + user_email
    "sentry.organizationmemberinvite",  # email
    "sentry.authidentity",            # ident (SSO identity, ~always an email)
    # Config records that embed emails in generic text fields.
    "sentry.alertruletriggeraction",  # target_identifier / target_display
    "sentry.notificationaction",      # target_identifier / target_display
    "sentry.projectownership",        # raw (ownership rules with emails)
]

# Matches typical email addresses for scrubbing and the safety scan.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Blanket mode: every email found in the kept records is replaced with this.
DEFAULT_PLACEHOLDER = "chris.stavitsky@sentry.io"

# Domain mode: the domain that scrubbed emails are rewritten to (local part is preserved).
DEFAULT_REPLACEMENT_DOMAIN = "example.com"


def scrub_emails(value, placeholder):
    """Recursively replace every email-like string inside a JSON value
    (dict/list/str) with the placeholder. Returns (new_value, count)."""
    if isinstance(value, str):
        # Don't count the placeholder replacing itself.
        new_value, n = EMAIL_RE.subn(
            lambda m: m.group(0) if m.group(0) == placeholder else placeholder,
            value,
        )
        return new_value, sum(1 for _ in EMAIL_RE.finditer(value)
                              if _.group(0) != placeholder)
    if isinstance(value, list):
        count = 0
        out = []
        for item in value:
            new_item, n = scrub_emails(item, placeholder)
            out.append(new_item)
            count += n
        return out, count
    if isinstance(value, dict):
        count = 0
        out = {}
        for k, v in value.items():
            new_v, n = scrub_emails(v, placeholder)
            out[k] = new_v
            count += n
        return out, count
    return value, 0


def scan_for_emails(records, placeholder):
    """Return {model: count} for records whose serialized fields still
    contain a real (non-placeholder) email-like string."""
    hits = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        blob = json.dumps(record.get("fields", record))
        real = [m for m in EMAIL_RE.findall(blob) if m != placeholder]
        if real:
            model = record.get("model", "<unknown>")
            hits[model] = hits.get(model, 0) + len(real)
    return hits


def scrub_domains(value, domain_map):
    """Recursively rewrite the DOMAIN of any email whose domain (case-insensitive) is a key in
    domain_map, preserving the local part. domain_map: {target_domain_lower: replacement_domain}.
    Returns (new_value, count)."""
    if isinstance(value, str):
        counter = {"n": 0}

        def repl(m):
            local, _, domain = m.group(0).rpartition("@")
            new_domain = domain_map.get(domain.lower())
            if new_domain:
                counter["n"] += 1
                return f"{local}@{new_domain}"
            return m.group(0)

        new_value = EMAIL_RE.sub(repl, value)
        return new_value, counter["n"]
    if isinstance(value, list):
        out, count = [], 0
        for item in value:
            new_item, n = scrub_domains(item, domain_map)
            out.append(new_item)
            count += n
        return out, count
    if isinstance(value, dict):
        out, count = {}, 0
        for k, v in value.items():
            new_v, n = scrub_domains(v, domain_map)
            out[k] = new_v
            count += n
        return out, count
    return value, 0


def scan_for_domain(records, domains):
    """Return {model: count} for records still containing an email at one of the target domains."""
    dset = {d.lower() for d in domains}
    hits = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        blob = json.dumps(record.get("fields", record))
        bad = [m for m in EMAIL_RE.findall(blob) if m.rpartition("@")[2].lower() in dset]
        if bad:
            model = record.get("model", "<unknown>")
            hits[model] = hits.get(model, 0) + len(bad)
    return hits


def default_output_path(input_path: str) -> str:
    """Insert '_minus_members' before the .json extension."""
    root, ext = os.path.splitext(input_path)
    if ext.lower() != ".json":
        # No .json extension: just append the suffix.
        return input_path + "_minus_members"
    return f"{root}_minus_members{ext}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="Path to the export JSON file")
    parser.add_argument("-o", "--output",
                        help="Output path (default: <input>_minus_members.json)")
    parser.add_argument("--models", nargs="+", default=DEFAULT_MODELS,
                        help=f"Model names to remove (default: {' '.join(DEFAULT_MODELS)})")
    parser.add_argument("--placeholder", default=DEFAULT_PLACEHOLDER,
                        help=f"Blanket mode: email to substitute for any survivor (default: {DEFAULT_PLACEHOLDER})")
    parser.add_argument("--email_domain_to_scrub", action="append", default=None, metavar="DOMAIN",
                        help="Rewrite only the DOMAIN of emails at DOMAIN, keeping the local part "
                             "(e.g. --email_domain_to_scrub=corp.com turns alice@corp.com into "
                             "alice@example.com). Repeatable, or comma-separated. When set, this "
                             "replaces the blanket --placeholder scrub.")
    parser.add_argument("--replacement-domain", dest="replacement_domain",
                        default=DEFAULT_REPLACEMENT_DOMAIN, metavar="DOMAIN",
                        help=f"Domain to substitute in domain-scrub mode (default: {DEFAULT_REPLACEMENT_DOMAIN})")
    parser.add_argument("--no-scrub", action="store_true",
                        help="Skip the email-scrub pass entirely (only remove records)")
    args = parser.parse_args()

    if not os.path.isfile(args.input):
        print(f"Error: file not found: {args.input}", file=sys.stderr)
        return 1

    try:
        with open(args.input, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error: {args.input} is not valid JSON: {e}", file=sys.stderr)
        return 1

    if not isinstance(data, list):
        print("Error: expected the export to be a JSON array of records.",
              file=sys.stderr)
        return 1

    remove = set(args.models)
    kept, removed_counts = [], {}
    for record in data:
        model = record.get("model") if isinstance(record, dict) else None
        if model in remove:
            removed_counts[model] = removed_counts.get(model, 0) + 1
        else:
            kept.append(record)

    # Email scrub on the kept records. Two mutually-exclusive modes:
    #   * domain mode (--email_domain_to_scrub): rewrite only the DOMAIN of matching emails,
    #     preserving the local part (alice@corp.com -> alice@example.com);
    #   * blanket mode (default): replace every email with a single placeholder.
    scrub_domain_list = []
    for entry in (args.email_domain_to_scrub or []):
        scrub_domain_list.extend(d.strip().lstrip("@").lower()
                                 for d in entry.split(",") if d.strip())

    scrubbed_count = 0
    domain_scrubbed_count = 0
    if args.no_scrub:
        pass
    elif scrub_domain_list:
        domain_map = {d: args.replacement_domain for d in scrub_domain_list}
        kept, domain_scrubbed_count = scrub_domains(kept, domain_map)
    else:
        kept, scrubbed_count = scrub_emails(kept, args.placeholder)

    output_path = args.output or default_output_path(args.input)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(kept, f, indent=2)

    total_removed = sum(removed_counts.values())
    print(f"Read {len(data)} records from {args.input}")
    if removed_counts:
        for model in sorted(removed_counts):
            print(f"  removed {removed_counts[model]} x {model}")
    else:
        print(f"  removed 0 (no records matched: {', '.join(sorted(remove))})")
    print(f"Wrote {len(kept)} records ({total_removed} removed) to {output_path}")
    if args.no_scrub:
        pass
    elif scrub_domain_list:
        print(f"Domain-scrubbed {domain_scrubbed_count} email(s): "
              f"@{{{', '.join(scrub_domain_list)}}} -> @{args.replacement_domain}")
    else:
        print(f"Scrubbed {scrubbed_count} email(s) -> {args.placeholder}")

    # Safety net.
    if args.no_scrub:
        pass
    elif scrub_domain_list:
        remaining = scan_for_domain(kept, scrub_domain_list)
        if remaining:
            print("\nWARNING: emails at the scrubbed domain(s) still present in the output:",
                  file=sys.stderr)
            for model in sorted(remaining):
                print(f"  {remaining[model]} in {model}", file=sys.stderr)
        else:
            print(f"Domain scan: no @{{{', '.join(scrub_domain_list)}}} addresses remain in the output.")
            print("(Note: emails at OTHER domains are intentionally left unchanged in domain mode.)")
    else:
        # Blanket mode: warn if any real (non-placeholder) email survives.
        remaining = scan_for_emails(kept, args.placeholder)
        if remaining:
            print("\nWARNING: real email-like strings still present in the output:",
                  file=sys.stderr)
            for model in sorted(remaining):
                print(f"  {remaining[model]} in {model}", file=sys.stderr)
        else:
            print("Email scan: no real email addresses remain in the output.")
    return 0


import os as _rl_os, sys as _rl_sys
_rl_sys.path.insert(0, _rl_os.path.join(_rl_os.path.dirname(_rl_os.path.abspath(__file__)), "..", "common"))
_rl_sys.path.insert(0, _rl_os.path.join(_rl_os.path.dirname(_rl_os.path.abspath(__file__)), "common"))
from run_logging import start_run_log


if __name__ == "__main__":
    start_run_log("strip_members")
    sys.exit(main())
