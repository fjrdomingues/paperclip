#!/usr/bin/env python3
"""
Daily Outreach Summary
Reads sent_log.csv, queries Twilio for inbound WhatsApp replies,
and sends a morning summary to Telegram.

Usage:
  python daily_summary.py [--dry-run] [--date YYYY-MM-DD]

Env vars (loaded from projects/telegram/.env):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
  TWILIO_ACCOUNT_SID, TWILIO_API_KEY_SID, TWILIO_API_KEY_SECRET, TWILIO_WHATSAPP_FROM
"""

import argparse
import base64
import csv
import json
import os
import sys
from datetime import date, datetime, timedelta
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
PROJECT_ROOT = SCRIPT_DIR.parent.parent.parent
SENT_LOG_FILE = PROJECT_ROOT / "projects" / "whatsapp" / "data" / "sent_log.csv"
LEADS_FILE = PROJECT_ROOT / "projects" / "growth" / "data" / "leads.csv"
ENV_FILE = PROJECT_ROOT / "projects" / "telegram" / ".env"

DEFAULT_CHAT_ID = "528866003"
DAILY_CAP = 30


def load_env():
    if ENV_FILE.exists():
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def query_twilio_inbound(account_sid, api_key_sid, api_key_secret, report_date: str) -> list:
    """Query Twilio for all inbound WhatsApp messages on the given date."""
    auth = base64.b64encode(f"{api_key_sid}:{api_key_secret}".encode()).decode()
    next_date = (datetime.strptime(report_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    params = parse.urlencode({
        "Direction": "inbound",
        "DateSent>": report_date,
        "DateSent<": next_date,
        "PageSize": 1000,
    })
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json?{params}"
    req = request.Request(url)
    req.add_header("Authorization", f"Basic {auth}")

    try:
        with request.urlopen(req, timeout=20, context=_SSL_CTX) as resp:
            data = json.loads(resp.read())
            return data.get("messages", [])
    except HTTPError as e:
        print(f"WARN: Twilio API {e.code}: {e.read().decode()[:200]}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"WARN: Twilio query failed: {e}", file=sys.stderr)
        return []


def load_sent_log() -> list:
    if not SENT_LOG_FILE.exists():
        return []
    with open(SENT_LOG_FILE, newline="") as f:
        return list(csv.DictReader(f))


def load_leads() -> list:
    if not LEADS_FILE.exists():
        return []
    with open(LEADS_FILE, newline="") as f:
        return list(csv.DictReader(f))


def normalize_phone(phone: str) -> str:
    """Strip whatsapp: prefix and leading zeros for comparison."""
    return phone.replace("whatsapp:", "").strip()


def build_summary(report_date: str, twilio_messages: list, sent_entries: list, leads: list) -> str:
    # Yesterday's sends
    sent_yesterday = [e for e in sent_entries
                      if e.get("sent_at", "").startswith(report_date) and e.get("status") == "sent"]
    failed_yesterday = [e for e in sent_entries
                        if e.get("sent_at", "").startswith(report_date) and e.get("status") != "sent"]

    sent_phones_normalized = {normalize_phone(e["phone"]) for e in sent_yesterday}

    # Inbound replies from leads we messaged
    inbound_from_leads = [
        m for m in twilio_messages
        if normalize_phone(m.get("from", "")) in sent_phones_normalized
    ]

    # Pipeline stats (all time)
    all_sent_phones = {normalize_phone(e["phone"]) for e in sent_entries if e.get("status") == "sent"}
    leads_with_phone = [l for l in leads if l.get("phone", "").strip()]
    leads_remaining = [l for l in leads_with_phone
                       if normalize_phone(l["phone"]) not in all_sent_phones]

    today_planned = min(DAILY_CAP, len(leads_remaining))

    lines = [
        f"☀️ *Daily Outreach Summary — {report_date}*",
        "",
        f"*📤 Sent yesterday:* {len(sent_yesterday)}",
    ]
    if failed_yesterday:
        lines.append(f"*❌ Failed:* {len(failed_yesterday)}")

    lines += [
        f"*📥 Inbound replies (Twilio):* {len(twilio_messages)} total / {len(inbound_from_leads)} from leads",
        "",
        "*📊 Pipeline:*",
        f"  • Total leads: {len(leads_with_phone)}",
        f"  • Sent all-time: {len(all_sent_phones)}",
        f"  • Remaining: {len(leads_remaining)}",
        "",
        f"*📅 Today's planned batch:* {today_planned} messages",
    ]

    return "\n".join(lines)


def send_telegram(text: str, bot_token: str, chat_id: str) -> bool:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }).encode()
    req = request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
            result = json.loads(resp.read())
            return result.get("ok", False)
    except HTTPError as e:
        print(f"ERROR: Telegram API {e.code}: {e.read().decode()[:200]}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description="Daily Outreach Summary")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print summary without sending to Telegram")
    parser.add_argument("--date", default=(date.today() - timedelta(days=1)).isoformat(),
                        help="Report date (YYYY-MM-DD, default: yesterday)")
    args = parser.parse_args()

    load_env()

    account_sid = os.environ.get("TWILIO_ACCOUNT_SID", "")
    api_key_sid = os.environ.get("TWILIO_API_KEY_SID", "")
    api_key_secret = os.environ.get("TWILIO_API_KEY_SECRET", "")

    sent_entries = load_sent_log()
    leads = load_leads()

    twilio_messages = []
    if account_sid and api_key_sid and api_key_secret:
        twilio_messages = query_twilio_inbound(account_sid, api_key_sid, api_key_secret, args.date)
    else:
        print("WARN: Twilio credentials missing — skipping inbound query", file=sys.stderr)

    summary = build_summary(args.date, twilio_messages, sent_entries, leads)

    if args.dry_run:
        print(summary)
        return

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        print("ERROR: TELEGRAM_BOT_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    chat_id = os.environ.get("TELEGRAM_CHAT_ID", DEFAULT_CHAT_ID)
    ok = send_telegram(summary, bot_token, chat_id)
    if ok:
        print(f"Summary sent for {args.date}.")
    else:
        print("ERROR: Failed to send summary.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
