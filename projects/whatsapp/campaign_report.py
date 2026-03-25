#!/usr/bin/env python3
"""
WhatsApp Campaign Daily Report
Reads sent_log.csv and inbox.jsonl, builds a campaign summary, sends via Telegram.

Usage:
  python campaign_report.py [--dry-run] [--date YYYY-MM-DD]

Env vars (loaded from projects/telegram/.env):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (defaults to 528866003)
"""

import argparse
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from urllib import request, parse
from urllib.error import HTTPError

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
SENT_LOG_FILE = SCRIPT_DIR / "data" / "sent_log.csv"
INBOX_FILE = SCRIPT_DIR / "data" / "inbox.jsonl"
LEADS_FILE = PROJECT_ROOT / "projects" / "growth" / "data" / "leads.csv"
ENV_FILE = PROJECT_ROOT / "projects" / "telegram" / ".env"

DEFAULT_CHAT_ID = "528866003"

# Keyword heuristics for classifying replies
OPT_OUT_KEYWORDS = ["stop", "parar", "remover", "cancelar", "não quero", "nao quero",
                    "não tenho interesse", "nao tenho interesse", "não me contacte",
                    "nao me contacte", "descadastrar", "sair", "unsubscribe"]
INTERESTED_KEYWORDS = ["interesse", "interessado", "interessada", "sim", "quero saber",
                       "mais informação", "mais informacao", "quero", "pode enviar",
                       "como funciona", "fale mais", "diz-me mais", "conta-me mais"]
DEMO_KEYWORDS = ["reunião", "reuniao", "ligar", "chamada", "call", "demo", "agendar",
                 "marcar", "disponível", "disponivel", "quando podemos", "meet"]


def load_env():
    if ENV_FILE.exists():
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def classify_reply(body: str) -> str:
    """Return 'opt_out', 'demo', 'interested', or 'neutral'."""
    text = body.lower()
    if any(k in text for k in OPT_OUT_KEYWORDS):
        return "opt_out"
    if any(k in text for k in DEMO_KEYWORDS):
        return "demo"
    if any(k in text for k in INTERESTED_KEYWORDS):
        return "interested"
    return "neutral"


def load_sent_log(report_date: str) -> dict:
    """Load sent_log.csv. Returns dict: phone -> list of entries for report_date."""
    by_phone = defaultdict(list)
    if not SENT_LOG_FILE.exists():
        return by_phone
    with open(SENT_LOG_FILE, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            by_phone[row["phone"]].append(row)
    return by_phone


def load_inbox(report_date: str) -> list:
    """Load inbox.jsonl entries. Returns all entries (not date-filtered — we use all available)."""
    entries = []
    if not INBOX_FILE.exists():
        return entries
    with open(INBOX_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return entries


def load_leads() -> list:
    if not LEADS_FILE.exists():
        return []
    with open(LEADS_FILE, newline="") as f:
        reader = csv.DictReader(f)
        return list(reader)


def build_report(report_date: str) -> str:
    sent_log = load_sent_log(report_date)
    inbox = load_inbox(report_date)
    leads = load_leads()

    # --- Sent stats ---
    sent_today = []
    failed_today = []
    for phone, entries in sent_log.items():
        for e in entries:
            if e.get("sent_at", "").startswith(report_date):
                if e.get("status") == "sent":
                    sent_today.append(e)
                else:
                    failed_today.append(e)

    sent_phones = {e["phone"] for e in sent_today}

    # --- Inbox: replies from phones we messaged ---
    replied_phones = defaultdict(list)
    for msg in inbox:
        phone = msg.get("from", "").strip()
        if not phone.startswith("+"):
            phone = "+" + phone.lstrip("0")
        if phone in sent_phones:
            replied_phones[phone].append(msg)

    # Classify replies
    opt_outs = []
    demo_requests = []
    interested = []
    other_replies = []
    for phone, msgs in replied_phones.items():
        # Find the lead name
        name = next(
            (e["name"] for e in sent_log.get(phone, []) if e.get("name")), phone
        )
        for msg in msgs:
            body = msg.get("body", "")
            cls = classify_reply(body)
            entry = {"phone": phone, "name": name, "body": body, "cls": cls}
            if cls == "opt_out":
                opt_outs.append(entry)
            elif cls == "demo":
                demo_requests.append(entry)
            elif cls == "interested":
                interested.append(entry)
            else:
                other_replies.append(entry)

    # --- Pipeline stats ---
    all_sent_phones = set()
    for phone, entries in sent_log.items():
        if any(e.get("status") == "sent" for e in entries):
            all_sent_phones.add(phone)

    leads_with_phone = [l for l in leads if l.get("phone", "").strip()]
    leads_remaining = [
        l for l in leads_with_phone
        if l.get("phone", "").strip() not in all_sent_phones
    ]

    # Batch number: approximate by day (30/day cap)
    total_sent = len(all_sent_phones)
    batch_num = (total_sent // 30) + 1

    # --- Build message ---
    lines = []
    lines.append(f"📊 *WhatsApp Campaign Report — {report_date}*")
    lines.append("")

    lines.append(f"*📤 Sent today:* {len(sent_today)} (batch ~{batch_num})")
    if failed_today:
        lines.append(f"*❌ Failed today:* {len(failed_today)}")
    lines.append(f"*📥 Replies received:* {len(replied_phones)} unique contacts")
    lines.append("")

    lines.append(f"*🔁 Pipeline:* {len(leads_remaining)} leads remaining / {len(leads_with_phone)} total")
    lines.append(f"*📨 Total sent (all time):* {total_sent}")
    lines.append("")

    if demo_requests:
        lines.append(f"*🎯 Demo requests ({len(demo_requests)}):*")
        for e in demo_requests[:5]:
            snippet = e["body"][:80].replace("*", "").replace("_", "")
            lines.append(f"  • {e['name']}: _{snippet}_")
        lines.append("")

    if interested:
        lines.append(f"*✅ Interested ({len(interested)}):*")
        for e in interested[:5]:
            snippet = e["body"][:80].replace("*", "").replace("_", "")
            lines.append(f"  • {e['name']}: _{snippet}_")
        lines.append("")

    if opt_outs:
        lines.append(f"*🚫 Opt-outs ({len(opt_outs)}):*")
        for e in opt_outs[:5]:
            lines.append(f"  • {e['name']} ({e['phone']})")
        lines.append("")

    if other_replies:
        lines.append(f"*💬 Other replies:* {len(other_replies)}")
        for e in other_replies[:3]:
            snippet = e["body"][:60].replace("*", "").replace("_", "")
            lines.append(f"  • {e['name']}: _{snippet}_")
        lines.append("")

    if failed_today:
        lines.append(f"*⚠️ Delivery failures ({len(failed_today)}):*")
        for e in failed_today[:3]:
            lines.append(f"  • {e['name']} ({e['phone']}): {e.get('error', 'unknown')[:60]}")
        lines.append("")

    if not (demo_requests or interested or opt_outs or other_replies):
        lines.append("_No replies recorded yet._")

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
        with request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            return result.get("ok", False)
    except HTTPError as e:
        body = e.read().decode()
        print(f"ERROR: Telegram API returned {e.code}: {body}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description="WhatsApp Campaign Daily Report")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print report without sending to Telegram")
    parser.add_argument("--date", default=date.today().isoformat(),
                        help="Report date (YYYY-MM-DD, default: today)")
    args = parser.parse_args()

    load_env()

    report = build_report(args.date)

    if args.dry_run:
        print(report)
        return

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        print("ERROR: TELEGRAM_BOT_TOKEN is not set", file=sys.stderr)
        sys.exit(1)

    chat_id = os.environ.get("TELEGRAM_CHAT_ID", DEFAULT_CHAT_ID)

    ok = send_telegram(report, bot_token, chat_id)
    if ok:
        print(f"Report sent for {args.date}.")
    else:
        print("ERROR: Failed to send report.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
