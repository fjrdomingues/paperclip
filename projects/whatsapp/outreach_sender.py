#!/usr/bin/env python3
"""
WhatsApp Outreach Sender
Reads leads from projects/growth/data/leads.csv, sends initial outreach templates,
logs results to projects/whatsapp/data/sent_log.csv.

Usage:
  python outreach_sender.py [--template remodelar_initial_outreach] [--daily-cap 30] [--dry-run]

Env vars (loaded from projects/telegram/.env if present):
  TWILIO_ACCOUNT_SID, TWILIO_API_KEY_SID, TWILIO_API_KEY_SECRET, TWILIO_WHATSAPP_FROM
"""

import argparse
import csv
import json
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path
from urllib import request, parse
from urllib.error import HTTPError

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
LEADS_FILE = PROJECT_ROOT / "projects" / "growth" / "data" / "leads.csv"
TEMPLATES_FILE = SCRIPT_DIR / "templates.json"
SENT_LOG_FILE = SCRIPT_DIR / "data" / "sent_log.csv"
ENV_FILE = PROJECT_ROOT / "projects" / "telegram" / ".env"

RATE_LIMIT_DELAY = 1.5  # seconds between sends
SENT_LOG_FIELDS = ["phone", "name", "agency", "template_name", "template_sid", "sent_at", "twilio_sid", "status", "error"]


def load_env():
    if ENV_FILE.exists():
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def require_env(name):
    val = os.environ.get(name)
    if not val:
        print(f"ERROR: {name} is not set", file=sys.stderr)
        sys.exit(1)
    return val


def load_templates():
    with open(TEMPLATES_FILE) as f:
        data = json.load(f)
    return {t["name"]: t for t in data["templates"]}


def load_sent_log():
    """Return set of phones already sent to (successfully) for today's date."""
    sent = {}  # phone -> list of dicts
    if not SENT_LOG_FILE.exists():
        return sent
    with open(SENT_LOG_FILE, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            phone = row["phone"]
            if phone not in sent:
                sent[phone] = []
            sent[phone].append(row)
    return sent


def count_sent_today(sent_log):
    today = date.today().isoformat()
    count = 0
    for entries in sent_log.values():
        for e in entries:
            if e.get("sent_at", "").startswith(today) and e.get("status") == "sent":
                count += 1
    return count


def append_sent_log(entry):
    SENT_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    file_exists = SENT_LOG_FILE.exists()
    with open(SENT_LOG_FILE, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SENT_LOG_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(entry)


def ensure_whatsapp_prefix(phone):
    if not phone.startswith("whatsapp:"):
        return f"whatsapp:{phone}"
    return phone


def send_template(account_sid, api_key_sid, api_key_secret, from_number, to_number, content_sid, variables):
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    vars_json = json.dumps(variables)
    data = parse.urlencode({
        "From": from_number,
        "To": ensure_whatsapp_prefix(to_number),
        "ContentSid": content_sid,
        "ContentVariables": vars_json,
    }).encode()
    req = request.Request(url, data=data, method="POST")
    import base64
    credentials = base64.b64encode(f"{api_key_sid}:{api_key_secret}".encode()).decode()
    req.add_header("Authorization", f"Basic {credentials}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with request.urlopen(req) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        body = e.read().decode()
        return json.loads(body) if body else {"error_message": str(e), "status": "failed"}


def load_leads(leads_file=LEADS_FILE):
    if not Path(leads_file).exists():
        print(f"ERROR: Leads file not found: {leads_file}", file=sys.stderr)
        print("Run WIN-52 scraper first to populate leads.", file=sys.stderr)
        sys.exit(1)
    leads = []
    with open(leads_file, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            leads.append(row)
    return leads


def extract_first_name(full_name):
    """Extract first name for template variable."""
    parts = full_name.strip().split()
    return parts[0] if parts else full_name


def main():
    parser = argparse.ArgumentParser(description="WhatsApp Outreach Sender")
    parser.add_argument("--template", default="remodelar_initial_outreach",
                        help="Template name to use (default: remodelar_initial_outreach)")
    parser.add_argument("--daily-cap", type=int, default=30,
                        help="Max messages to send per day (default: 30)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be sent without actually sending")
    parser.add_argument("--leads-file", type=Path, default=LEADS_FILE,
                        help=f"Path to leads CSV (default: {LEADS_FILE})")
    args = parser.parse_args()

    load_env()

    if not args.dry_run:
        account_sid = require_env("TWILIO_ACCOUNT_SID")
        api_key_sid = require_env("TWILIO_API_KEY_SID")
        api_key_secret = require_env("TWILIO_API_KEY_SECRET")
        from_number = require_env("TWILIO_WHATSAPP_FROM")

    templates = load_templates()
    if args.template not in templates:
        print(f"ERROR: Template '{args.template}' not found in {TEMPLATES_FILE}", file=sys.stderr)
        print(f"Available templates: {', '.join(templates.keys())}", file=sys.stderr)
        sys.exit(1)

    template = templates[args.template]
    if template["status"] not in ("approved",):
        print(f"WARNING: Template '{args.template}' status is '{template['status']}' (not 'approved')")
        print("Meta may reject messages sent with unapproved templates.")

    leads = load_leads(args.leads_file)
    sent_log = load_sent_log()
    sent_today = count_sent_today(sent_log)

    print(f"Loaded {len(leads)} leads from {args.leads_file}")
    print(f"Template: {template['name']} (SID: {template['sid']}, status: {template['status']})")
    print(f"Sent today: {sent_today} / {args.daily_cap} daily cap")

    sent_count = 0
    skipped_count = 0
    failed_count = 0

    for lead in leads:
        phone = lead.get("phone", "").strip()
        name = lead.get("name", "").strip()
        agency = lead.get("agency", "").strip()

        if not phone:
            print(f"  SKIP (no phone): {name}")
            skipped_count += 1
            continue

        # Normalize phone (ensure + prefix)
        if not phone.startswith("+"):
            phone = "+" + phone.lstrip("0")

        # Skip if already sent this template to this phone
        if phone in sent_log:
            already_sent = any(
                e.get("template_name") == args.template and e.get("status") == "sent"
                for e in sent_log[phone]
            )
            if already_sent:
                print(f"  SKIP (already sent): {name} {phone}")
                skipped_count += 1
                continue

        # Enforce daily cap
        if sent_today + sent_count >= args.daily_cap:
            print(f"  STOP: Daily cap of {args.daily_cap} reached")
            break

        first_name = extract_first_name(name)
        variables = {"1": first_name}

        if args.dry_run:
            print(f"  [DRY RUN] Would send '{template['name']}' to {name} ({phone}) vars={variables}")
            sent_count += 1
            continue

        print(f"  Sending to {name} ({phone})...", end=" ", flush=True)
        resp = send_template(account_sid, api_key_sid, api_key_secret, from_number, phone,
                             template["sid"], variables)

        twilio_sid = resp.get("sid", "")
        status = resp.get("status", "")
        error = resp.get("message") or resp.get("error_message") or resp.get("code", "")

        entry = {
            "phone": phone,
            "name": name,
            "agency": agency,
            "template_name": args.template,
            "template_sid": template["sid"],
            "sent_at": datetime.utcnow().isoformat() + "Z",
            "twilio_sid": twilio_sid,
            "status": "sent" if status in ("queued", "sent") else "failed",
            "error": error if status not in ("queued", "sent") else "",
        }
        append_sent_log(entry)

        if status in ("queued", "sent"):
            print(f"OK (SID: {twilio_sid})")
            sent_count += 1
        else:
            print(f"FAILED (status={status}, error={error})")
            failed_count += 1

        time.sleep(RATE_LIMIT_DELAY)

    print()
    print(f"Done: {sent_count} sent, {skipped_count} skipped, {failed_count} failed")
    print(f"Sent log: {SENT_LOG_FILE}")


if __name__ == "__main__":
    main()
