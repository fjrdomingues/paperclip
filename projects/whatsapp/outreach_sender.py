#!/usr/bin/env python3
"""
WhatsApp Outreach Sender
Reads leads from projects/growth/data/leads.csv, sends initial outreach templates,
logs results to projects/whatsapp/data/sent_log.csv.

Usage:
  python outreach_sender.py [--template remodelar_initial_outreach] [--daily-cap 30] [--dry-run]
  python outreach_sender.py --one-shot [--template ...] [--daily-cap 50]

  --one-shot: Send at most 1 message per invocation, respecting a 2-5 min random
              interval tracked in data/sender_state.json. Designed for cron use
              (run every 60s; actual sends happen every 2-5 min via state file).

Env vars (loaded from projects/telegram/.env if present):
  TWILIO_ACCOUNT_SID, TWILIO_API_KEY_SID, TWILIO_API_KEY_SECRET, TWILIO_WHATSAPP_FROM
  PAPERCLIP_CEO_API_KEY  (optional) used to post Paperclip alert on rate-limit errors
  PAPERCLIP_API_URL      (optional, default http://localhost:3100)
"""

import argparse
import csv
import json
import os
import random
import sys
import time
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
import ssl
from urllib import request, parse
from urllib.error import HTTPError

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

import db as whatsapp_db

LISBON_TZ = ZoneInfo("Europe/Lisbon")
BUSINESS_HOURS_START = 9   # 09:00
BUSINESS_HOURS_END = 18    # 18:00

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
LEADS_FILE = PROJECT_ROOT / "projects" / "growth" / "data" / "leads.csv"
TEMPLATES_FILE = SCRIPT_DIR / "templates.json"
SENT_LOG_FILE = SCRIPT_DIR / "data" / "sent_log.csv"
ENV_FILE = PROJECT_ROOT / "projects" / "telegram" / ".env"

RATE_LIMIT_DELAY = 1.5  # seconds between sends (batch mode)
ONE_SHOT_MIN_DELAY = 120   # seconds minimum between one-shot sends (2 min)
ONE_SHOT_MAX_DELAY = 300   # seconds maximum between one-shot sends (5 min)
SENDER_STATE_FILE = SCRIPT_DIR / "data" / "sender_state.json"

# Error codes that indicate Meta rate-limiting / spam blocking
RATE_LIMIT_ERROR_CODES = {63112, 63114, 63116, 21611}

SENT_LOG_FIELDS = ["phone", "name", "agency", "template_name", "template_sid", "sent_at", "twilio_sid", "status", "error"]

PAPERCLIP_API_URL = os.environ.get("PAPERCLIP_API_URL", "http://localhost:3100")
WIN75_ISSUE_ID = "117b8e31-b203-42e6-b57e-63651eca6a69"


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


def is_business_hours():
    """Return True if current Lisbon time is within 09:00-18:00 WET/WEST."""
    now_lisbon = datetime.now(LISBON_TZ)
    return BUSINESS_HOURS_START <= now_lisbon.hour < BUSINESS_HOURS_END


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
        with request.urlopen(req, context=_SSL_CTX) as resp:
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


def load_sender_state():
    """Load one-shot sender state. Returns dict with defaults if missing."""
    defaults = {"next_send_at": None, "paused": False, "paused_at": None, "paused_reason": None}
    if not SENDER_STATE_FILE.exists():
        return defaults
    try:
        with open(SENDER_STATE_FILE) as f:
            data = json.load(f)
        return {**defaults, **data}
    except (json.JSONDecodeError, OSError):
        return defaults


def save_sender_state(state):
    SENDER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SENDER_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def schedule_next_send(state):
    """Set next_send_at to now + random 2-5 min interval."""
    delay = random.randint(ONE_SHOT_MIN_DELAY, ONE_SHOT_MAX_DELAY)
    next_time = datetime.now(timezone.utc) + timedelta(seconds=delay)
    state["next_send_at"] = next_time.isoformat()
    return delay


def is_time_to_send(state):
    """Return True if next_send_at is unset or in the past."""
    if not state.get("next_send_at"):
        return True
    try:
        next_send = datetime.fromisoformat(state["next_send_at"])
        return datetime.now(timezone.utc) >= next_send
    except ValueError:
        return True


def post_paperclip_alert(error_code, error_msg, phone, template_name):
    """Post a comment on WIN-75 using the CEO API key to alert the Chief."""
    api_key = os.environ.get("PAPERCLIP_CEO_API_KEY", "")
    if not api_key:
        print(f"  [ALERT] No PAPERCLIP_CEO_API_KEY — cannot post Paperclip alert.", file=sys.stderr)
        return
    comment = (
        f"## WhatsApp Outreach Paused — Rate Limit Error\n\n"
        f"Automated alert from `outreach_sender.py --one-shot`:\n\n"
        f"- **Error code:** `{error_code}`\n"
        f"- **Error message:** {error_msg}\n"
        f"- **Phone:** `{phone}`\n"
        f"- **Template:** `{template_name}`\n"
        f"- **Action taken:** All further sends paused (`sender_state.json` `paused=true`).\n\n"
        f"Chief: please investigate and manually reset `paused` to `false` in "
        f"`projects/whatsapp/data/sender_state.json` when it is safe to resume."
    )
    payload = json.dumps({"body": comment}).encode()
    url = f"{PAPERCLIP_API_URL}/api/issues/{WIN75_ISSUE_ID}/comments"
    req = request.Request(url, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    try:
        with request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
            print(f"  [ALERT] Paperclip alert posted (status {resp.status}).")
    except Exception as exc:
        print(f"  [ALERT] Failed to post Paperclip alert: {exc}", file=sys.stderr)


def run_one_shot(args, account_sid, api_key_sid, api_key_secret, from_number, templates):
    """Send at most 1 message, respecting 2-5 min random intervals via state file."""
    state = load_sender_state()

    if state.get("paused"):
        print(f"Outreach is PAUSED (reason: {state.get('paused_reason', 'unknown')}). Exiting.")
        sys.exit(0)

    if not is_business_hours():
        now_lisbon = datetime.now(LISBON_TZ)
        print(f"Outside business hours ({now_lisbon.strftime('%H:%M %Z')}). Exiting.")
        sys.exit(0)

    if not is_time_to_send(state):
        next_send = state["next_send_at"]
        print(f"Not time yet. Next send scheduled at {next_send}. Exiting.")
        sys.exit(0)

    template = templates[args.template]
    leads = load_leads(args.leads_file)
    sent_log = load_sent_log()
    sent_today = count_sent_today(sent_log)

    if sent_today >= args.daily_cap:
        print(f"Daily cap reached ({sent_today}/{args.daily_cap}). Exiting.")
        sys.exit(0)

    NON_PROSPECTABLE_STATUSES = {"cliente", "client", "customer", "do_not_contact"}

    # Find next unsent lead
    target_lead = None
    for lead in leads:
        phone = lead.get("phone", "").strip()
        if not phone:
            continue
        if lead.get("status", "").strip().lower() in NON_PROSPECTABLE_STATUSES:
            continue
        if not phone.startswith("+"):
            phone = "+" + phone.lstrip("0")
        if phone in sent_log:
            already_sent = any(
                e.get("template_name") == args.template and e.get("status") == "sent"
                for e in sent_log[phone]
            )
            if already_sent:
                continue
        target_lead = {**lead, "phone": phone}
        break

    if target_lead is None:
        print("No unsent leads remaining. Exiting.")
        sys.exit(0)

    phone = target_lead["phone"]
    name = target_lead.get("name", "").strip()
    agency = target_lead.get("agency", "").strip()
    first_name = extract_first_name(name)
    variables = {"1": first_name}

    if args.dry_run:
        print(f"[DRY RUN] Would send '{template['name']}' to {name} ({phone}) vars={variables}")
        delay = schedule_next_send(state)
        save_sender_state(state)
        print(f"Next send scheduled in {delay}s.")
        return

    print(f"Sending to {name} ({phone})...", end=" ", flush=True)
    resp = send_template(account_sid, api_key_sid, api_key_secret, from_number, phone,
                         template["sid"], variables)

    twilio_sid = resp.get("sid", "")
    status = resp.get("status", "")
    error_msg = resp.get("message") or resp.get("error_message") or ""
    error_code = resp.get("code") or resp.get("error_code") or 0
    try:
        error_code = int(error_code)
    except (ValueError, TypeError):
        error_code = 0

    entry = {
        "phone": phone,
        "name": name,
        "agency": agency,
        "template_name": args.template,
        "template_sid": template["sid"],
        "sent_at": datetime.utcnow().isoformat() + "Z",
        "twilio_sid": twilio_sid,
        "status": "sent" if status in ("queued", "sent") else "failed",
        "error": error_msg if status not in ("queued", "sent") else "",
    }
    append_sent_log(entry)
    # Dual-write to SQLite
    _db = whatsapp_db.get_db()
    whatsapp_db.add_outreach_message(_db, entry["phone"], entry["template_name"],
                                     entry["template_sid"], entry["twilio_sid"],
                                     entry["status"], entry["error"], entry["sent_at"])
    whatsapp_db.update_contact_stage(_db, entry["phone"], "contacted", "system")
    _db.commit()
    _db.close()

    if status in ("queued", "sent"):
        print(f"OK (SID: {twilio_sid})")
        delay = schedule_next_send(state)
        save_sender_state(state)
        print(f"Sent today: {sent_today + 1}/{args.daily_cap}. Next send in {delay}s.")
    else:
        print(f"FAILED (status={status}, code={error_code}, error={error_msg})")
        if error_code in RATE_LIMIT_ERROR_CODES:
            state["paused"] = True
            state["paused_at"] = datetime.utcnow().isoformat() + "Z"
            state["paused_reason"] = f"Error {error_code}: {error_msg}"
            save_sender_state(state)
            print(f"RATE LIMIT ERROR {error_code} — outreach PAUSED. Posting Paperclip alert.")
            post_paperclip_alert(error_code, error_msg, phone, args.template)
        else:
            # Non-rate-limit failure: still schedule next send
            delay = schedule_next_send(state)
            save_sender_state(state)
            print(f"Non-critical failure. Next attempt in {delay}s.")


def main():
    parser = argparse.ArgumentParser(description="WhatsApp Outreach Sender")
    parser.add_argument("--template", default="remodelar_agentes_outreach",
                        help="Template name to use (default: remodelar_agentes_outreach)")
    parser.add_argument("--daily-cap", type=int, default=50,
                        help="Max messages to send per day (default: 50)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be sent without actually sending")
    parser.add_argument("--leads-file", type=Path, default=LEADS_FILE,
                        help=f"Path to leads CSV (default: {LEADS_FILE})")
    parser.add_argument("--one-shot", action="store_true",
                        help="Send at most 1 message per run; tracks timing in sender_state.json")
    args = parser.parse_args()

    load_env()

    if not args.dry_run:
        account_sid = require_env("TWILIO_ACCOUNT_SID")
        api_key_sid = require_env("TWILIO_API_KEY_SID")
        api_key_secret = require_env("TWILIO_API_KEY_SECRET")
        from_number = require_env("TWILIO_WHATSAPP_FROM")
    else:
        account_sid = api_key_sid = api_key_secret = from_number = ""

    templates = load_templates()
    if args.template not in templates:
        print(f"ERROR: Template '{args.template}' not found in {TEMPLATES_FILE}", file=sys.stderr)
        print(f"Available templates: {', '.join(templates.keys())}", file=sys.stderr)
        sys.exit(1)

    if args.one_shot:
        run_one_shot(args, account_sid, api_key_sid, api_key_secret, from_number, templates)
        return

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

    now_lisbon = datetime.now(LISBON_TZ)
    in_hours = is_business_hours()
    print(f"Business-hours check: {now_lisbon.strftime('%Y-%m-%d %H:%M %Z')} — {'OPEN (09:00-18:00)' if in_hours else 'CLOSED (outside 09:00-18:00)'}")
    if not in_hours and not args.dry_run:
        next_open = now_lisbon.replace(hour=BUSINESS_HOURS_START, minute=0, second=0, microsecond=0)
        if now_lisbon.hour >= BUSINESS_HOURS_END:
            next_open = next_open + timedelta(days=1)
        print(f"Outside business hours. Next window opens at {next_open.strftime('%Y-%m-%d %H:%M %Z')}. Exiting.")
        sys.exit(0)

    NON_PROSPECTABLE_STATUSES = {"cliente", "client", "customer", "do_not_contact"}

    for lead in leads:
        phone = lead.get("phone", "").strip()
        name = lead.get("name", "").strip()
        agency = lead.get("agency", "").strip()

        if not phone:
            print(f"  SKIP (no phone): {name}")
            skipped_count += 1
            continue

        if lead.get("status", "").strip().lower() in NON_PROSPECTABLE_STATUSES:
            print(f"  SKIP (client status): {name} {phone}")
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
        # Dual-write to SQLite
        _db = whatsapp_db.get_db()
        whatsapp_db.add_outreach_message(_db, entry["phone"], entry["template_name"],
                                         entry["template_sid"], entry["twilio_sid"],
                                         entry["status"], entry["error"], entry["sent_at"])
        whatsapp_db.update_contact_stage(_db, entry["phone"], "contacted", "system")
        _db.commit()
        _db.close()

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
