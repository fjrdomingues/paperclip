#!/usr/bin/env python3
"""
WhatsApp Follow-up Engine (WIN-58)
Tracks outreach responses and triggers automated follow-ups.

Behavior:
- Reads sent_log.csv (from outreach_sender.py) for initial outreach records
- Reads inbox.jsonl (from poll-inbox.sh) for inbound replies
- For opt-outs: marks as do-not-contact
- For interested replies: adds to review_queue.csv for human review
- For non-responders after FOLLOWUP_DELAY_HOURS: sends follow-up template
- Logs all state transitions to followup_log.csv
- Tracks per-contact state in followup_state.json

Usage:
  python followup_engine.py [--dry-run] [--followup-delay-hours 48]

Env vars (loaded from projects/telegram/.env):
  TWILIO_ACCOUNT_SID, TWILIO_API_KEY_SID, TWILIO_API_KEY_SECRET, TWILIO_WHATSAPP_FROM
"""

import argparse
import base64
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import ssl
from urllib import request, parse
from urllib.error import HTTPError

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
ENV_FILE = PROJECT_ROOT / "projects" / "telegram" / ".env"
DATA_DIR = SCRIPT_DIR / "data"
TEMPLATES_FILE = SCRIPT_DIR / "templates.json"

SENT_LOG_FILE = DATA_DIR / "sent_log.csv"
INBOX_FILE = DATA_DIR / "inbox.jsonl"
FOLLOWUP_STATE_FILE = DATA_DIR / "followup_state.json"
FOLLOWUP_LOG_FILE = DATA_DIR / "followup_log.csv"
DNC_FILE = DATA_DIR / "dnc.csv"
REVIEW_QUEUE_FILE = DATA_DIR / "review_queue.csv"

FOLLOWUP_LOG_FIELDS = ["phone", "name", "agency", "event", "detail", "timestamp"]
DNC_FIELDS = ["phone", "name", "agency", "reason", "reply_body", "added_at"]
REVIEW_QUEUE_FIELDS = ["phone", "name", "agency", "reply_body", "reply_at", "added_at"]

import db as whatsapp_db

RATE_LIMIT_DELAY = 1.5  # seconds between sends


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


def now_utc():
    return datetime.now(timezone.utc)


def parse_iso(ts_str):
    """Parse ISO-8601 UTC timestamp string to datetime."""
    if not ts_str:
        return None
    try:
        # Handle Z suffix
        ts_str = ts_str.replace("Z", "+00:00")
        return datetime.fromisoformat(ts_str)
    except ValueError:
        return None


def load_sent_log():
    """Load sent_log.csv. Returns dict: phone -> list of sent records."""
    records = {}
    if not SENT_LOG_FILE.exists():
        return records
    with open(SENT_LOG_FILE, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            phone = row.get("phone", "").strip()
            if not phone:
                continue
            if phone not in records:
                records[phone] = []
            records[phone].append(row)
    return records


def load_inbox():
    """Load inbox.jsonl. Returns dict: phone -> list of message dicts.

    Deduplicates by Twilio SID (field: "sid") to tolerate repeated append cycles
    in the raw JSONL file. First-seen record wins.
    """
    messages = {}
    seen_sids: set = set()
    if not INBOX_FILE.exists():
        return messages
    with open(INBOX_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                sid = msg.get("sid", "")
                if sid and sid in seen_sids:
                    continue
                if sid:
                    seen_sids.add(sid)
                phone = msg.get("from", "").strip()
                if not phone.startswith("+"):
                    phone = "+" + phone.lstrip("+")
                if phone not in messages:
                    messages[phone] = []
                messages[phone].append(msg)
            except json.JSONDecodeError:
                continue
    return messages


def load_state():
    """Load followup state JSON. Returns dict: phone -> state dict."""
    if not FOLLOWUP_STATE_FILE.exists():
        return {}
    with open(FOLLOWUP_STATE_FILE) as f:
        return json.load(f)


def save_state(state):
    """Save followup state JSON atomically."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = FOLLOWUP_STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.rename(FOLLOWUP_STATE_FILE)


def load_dnc():
    """Load do-not-contact list. Returns set of phone numbers."""
    dnc = set()
    if not DNC_FILE.exists():
        return dnc
    with open(DNC_FILE, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            phone = row.get("phone", "").strip()
            if phone:
                dnc.add(phone)
    return dnc


def append_csv(file_path, fields, row):
    """Append a row to a CSV file, writing header if new."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    file_exists = file_path.exists()
    with open(file_path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        if not file_exists:
            writer.writeheader()
        writer.writerow({k: row.get(k, "") for k in fields})


def log_event(phone, name, agency, event, detail=""):
    append_csv(FOLLOWUP_LOG_FILE, FOLLOWUP_LOG_FIELDS, {
        "phone": phone,
        "name": name,
        "agency": agency,
        "event": event,
        "detail": detail,
        "timestamp": now_utc().isoformat(),
    })


def add_to_dnc(phone, name, agency, reason, reply_body):
    append_csv(DNC_FILE, DNC_FIELDS, {
        "phone": phone,
        "name": name,
        "agency": agency,
        "reason": reason,
        "reply_body": reply_body,
        "added_at": now_utc().isoformat(),
    })


def add_to_review_queue(phone, name, agency, reply_body, reply_at):
    append_csv(REVIEW_QUEUE_FILE, REVIEW_QUEUE_FIELDS, {
        "phone": phone,
        "name": name,
        "agency": agency,
        "reply_body": reply_body,
        "reply_at": reply_at,
        "added_at": now_utc().isoformat(),
    })


def send_template(account_sid, api_key_sid, api_key_secret, from_number, to_number, content_sid, variables):
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    vars_json = json.dumps(variables)
    data = parse.urlencode({
        "From": from_number,
        "To": f"whatsapp:{to_number}" if not to_number.startswith("whatsapp:") else to_number,
        "ContentSid": content_sid,
        "ContentVariables": vars_json,
    }).encode()
    req = request.Request(url, data=data, method="POST")
    credentials = base64.b64encode(f"{api_key_sid}:{api_key_secret}".encode()).decode()
    req.add_header("Authorization", f"Basic {credentials}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with request.urlopen(req, context=_SSL_CTX) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        body = e.read().decode()
        return json.loads(body) if body else {"error_message": str(e), "status": "failed"}


def extract_first_name(full_name):
    parts = full_name.strip().split()
    return parts[0] if parts else full_name


def main():
    parser = argparse.ArgumentParser(description="WhatsApp Follow-up Engine")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen without sending or writing")
    parser.add_argument("--followup-delay-hours", type=int, default=48,
                        help="Hours after initial outreach before sending follow-up (default: 48)")
    args = parser.parse_args()

    load_env()

    if not args.dry_run:
        account_sid = require_env("TWILIO_ACCOUNT_SID")
        api_key_sid = require_env("TWILIO_API_KEY_SID")
        api_key_secret = require_env("TWILIO_API_KEY_SECRET")
        from_number = require_env("TWILIO_WHATSAPP_FROM")

    templates = load_templates()
    followup_template = templates.get("remodelar_agentes_followup")
    if not followup_template:
        print("ERROR: remodelar_agentes_followup template not found in templates.json", file=sys.stderr)
        sys.exit(1)

    if followup_template["status"] not in ("approved",):
        print(f"WARNING: remodelar_agentes_followup template status is '{followup_template['status']}' (not 'approved')")
        print("Meta may reject messages sent with unapproved templates.")

    sent_log = load_sent_log()
    inbox = load_inbox()
    state = load_state()
    dnc = load_dnc()

    cutoff = now_utc() - timedelta(hours=args.followup_delay_hours)

    stats = {"optout": 0, "interested": 0, "followup_sent": 0, "followup_skipped": 0, "already_processed": 0}

    print(f"Loaded {len(sent_log)} phones from sent_log, {sum(len(v) for v in inbox.values())} inbound messages")
    print(f"Follow-up delay: {args.followup_delay_hours}h (cutoff: {cutoff.isoformat()})")
    print(f"DNC list: {len(dnc)} entries")
    print()

    for phone, sent_entries in sent_log.items():
        # Only process phones that received initial outreach successfully
        initial_sends = [
            e for e in sent_entries
            if e.get("template_name") in ("remodelar_agentes_outreach", "remodelar_initial_outreach") and e.get("status") == "sent"
        ]
        if not initial_sends:
            continue

        # Use earliest initial send time
        initial_send = min(initial_sends, key=lambda e: e.get("sent_at", ""))
        name = initial_send.get("name", "")
        agency = initial_send.get("agency", "")
        sent_at = parse_iso(initial_send.get("sent_at", ""))
        first_name = extract_first_name(name)

        contact_state = state.get(phone, {})

        # Skip if already fully processed (DNC or follow-up sent and reply classified)
        if phone in dnc:
            stats["already_processed"] += 1
            continue

        # --- Check for inbound replies ---
        replies = inbox.get(phone, [])

        # Sort replies by timestamp, only consider replies after initial send
        valid_replies = []
        if sent_at:
            for msg in replies:
                reply_ts = parse_iso(msg.get("timestamp", ""))
                if reply_ts and reply_ts > sent_at:
                    valid_replies.append(msg)
        valid_replies.sort(key=lambda m: m.get("timestamp", ""))

        if valid_replies:
            # Look up the LLM-assigned stage from SQLite
            _db = whatsapp_db.get_db()
            stage_row = whatsapp_db.get_contact_stage(_db, phone)
            _db.close()
            stage = stage_row["stage"] if stage_row else "cold"

            if stage == "opted_out" and not contact_state.get("optout_processed"):
                body = valid_replies[0].get("body", "")
                print(f"  OPT-OUT: {name} ({phone}) — '{body[:60]}'")
                if not args.dry_run:
                    add_to_dnc(phone, name, agency, "opt-out reply", body)
                    log_event(phone, name, agency, "optout", f"Reply: {body[:100]}")
                    contact_state["optout_processed"] = True
                    contact_state["optout_at"] = now_utc().isoformat()
                    state[phone] = contact_state
                    dnc.add(phone)
                stats["optout"] += 1
                continue

            if stage in ("interested", "demo_requested") and not contact_state.get("interest_queued"):
                body = valid_replies[0].get("body", "")
                reply_at = valid_replies[0].get("timestamp", "")
                print(f"  INTERESTED: {name} ({phone}) — '{body[:60]}' → added to review queue")
                if not args.dry_run:
                    add_to_review_queue(phone, name, agency, body, reply_at)
                    log_event(phone, name, agency, "interested", f"Reply: {body[:100]}")
                    contact_state["interest_queued"] = True
                    contact_state["interest_at"] = now_utc().isoformat()
                    state[phone] = contact_state
                stats["interested"] += 1
                # No follow-up needed for interested leads
                continue

            if stage == "replied" and not contact_state.get("followup_sent") and not contact_state.get("interest_queued"):
                body = valid_replies[0].get("body", "")
                print(f"  REPLIED (other): {name} ({phone}) — '{body[:60]}' → no follow-up needed")
                if not args.dry_run:
                    log_event(phone, name, agency, "replied_other", f"Reply: {body[:100]}")
                    contact_state["replied_other"] = True
                    state[phone] = contact_state
                stats["followup_skipped"] += 1
                continue

            # stage in ("cold", "not_contacted") with valid_replies: LLM hasn't classified yet, treat as replied
            if stage in ("cold", "not_contacted") and not contact_state.get("followup_sent") and not contact_state.get("interest_queued"):
                body = valid_replies[0].get("body", "")
                print(f"  REPLIED (unclassified): {name} ({phone}) — '{body[:60]}' → no follow-up needed")
                if not args.dry_run:
                    log_event(phone, name, agency, "replied_other", f"Reply (unclassified): {body[:100]}")
                    contact_state["replied_other"] = True
                    state[phone] = contact_state
                stats["followup_skipped"] += 1
                continue

        # --- Check if follow-up already sent ---
        if contact_state.get("followup_sent"):
            stats["already_processed"] += 1
            continue

        # Check if we also already sent a follow-up in the sent_log
        followup_already_sent = any(
            e.get("template_name") in ("remodelar_agentes_followup", "remodelar_followup") and e.get("status") == "sent"
            for e in sent_entries
        )
        if followup_already_sent:
            if not args.dry_run:
                contact_state["followup_sent"] = True
                state[phone] = contact_state
            stats["already_processed"] += 1
            continue

        # --- Decide whether to send follow-up ---
        if not sent_at:
            stats["followup_skipped"] += 1
            continue

        if sent_at > cutoff:
            # Not yet 48h since initial outreach
            hours_remaining = (sent_at + timedelta(hours=args.followup_delay_hours) - now_utc()).total_seconds() / 3600
            print(f"  WAIT: {name} ({phone}) — {hours_remaining:.1f}h until follow-up eligible")
            stats["followup_skipped"] += 1
            continue

        # --- Send follow-up ---
        variables = {"1": first_name}

        if args.dry_run:
            print(f"  [DRY RUN] Would send follow-up to {name} ({phone})")
            stats["followup_sent"] += 1
            continue

        print(f"  Sending follow-up to {name} ({phone})...", end=" ", flush=True)
        resp = send_template(account_sid, api_key_sid, api_key_secret, from_number, phone,
                             followup_template["sid"], variables)

        twilio_sid = resp.get("sid", "")
        status = resp.get("status", "")
        error = resp.get("message") or resp.get("error_message") or resp.get("code", "")

        if status in ("queued", "sent"):
            print(f"OK (SID: {twilio_sid})")
            log_event(phone, name, agency, "followup_sent", f"SID: {twilio_sid}")
            contact_state["followup_sent"] = True
            contact_state["followup_sent_at"] = now_utc().isoformat()
            contact_state["followup_twilio_sid"] = twilio_sid
            state[phone] = contact_state
            stats["followup_sent"] += 1

            # Also append to sent_log for consistency
            sent_log_fields = ["phone", "name", "agency", "template_name", "template_sid", "sent_at", "twilio_sid", "status", "error"]
            entry = {
                "phone": phone,
                "name": name,
                "agency": agency,
                "template_name": "remodelar_agentes_followup",
                "template_sid": followup_template["sid"],
                "sent_at": now_utc().isoformat(),
                "twilio_sid": twilio_sid,
                "status": "sent",
                "error": "",
            }
            append_csv(SENT_LOG_FILE, sent_log_fields, entry)
        else:
            print(f"FAILED (status={status}, error={error})")
            log_event(phone, name, agency, "followup_failed", f"status={status} error={error}")
            stats["followup_skipped"] += 1

        time.sleep(RATE_LIMIT_DELAY)

    if not args.dry_run:
        save_state(state)

    print()
    print("=== Summary ===")
    print(f"  Opt-outs processed:   {stats['optout']}")
    print(f"  Interested (queued):  {stats['interested']}")
    print(f"  Follow-ups sent:      {stats['followup_sent']}")
    print(f"  Follow-ups skipped:   {stats['followup_skipped']}")
    print(f"  Already processed:    {stats['already_processed']}")
    if not args.dry_run:
        print(f"  State file: {FOLLOWUP_STATE_FILE}")
        print(f"  Log file:   {FOLLOWUP_LOG_FILE}")
        if DNC_FILE.exists():
            print(f"  DNC file:   {DNC_FILE}")
        if REVIEW_QUEUE_FILE.exists():
            print(f"  Review queue: {REVIEW_QUEUE_FILE}")


if __name__ == "__main__":
    main()
