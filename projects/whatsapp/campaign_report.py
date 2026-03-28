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
import json
import os
import sys
from datetime import date
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

DEFAULT_CHAT_ID = "528866003"

import db as whatsapp_db


def load_env():
    if ENV_FILE.exists():
        with open(ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


def build_report(report_date: str) -> str:
    conn = whatsapp_db.get_db()
    stats = whatsapp_db.get_pipeline_stats(conn)
    stages = stats["stages"]

    # Today's sent/failed
    today_stats = conn.execute(
        """SELECT
             SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) as sent,
             SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
           FROM outreach_messages WHERE DATE(sent_at) = ?""",
        (report_date,),
    ).fetchone()
    sent_today = today_stats["sent"] or 0
    failed_today = today_stats["failed"] or 0

    total_leads = stats["total_leads"]
    total_sent = stats["contacted"]
    total_replied = stats["replied"]
    leads_remaining = total_leads - total_sent
    batch_num = (total_sent // 30) + 1

    opt_outs = stages.get("opted_out", 0)
    demo_requests = stages.get("demo_requested", 0)
    interested_count = stages.get("interested", 0)

    # Recent classified replies for detail
    recent = conn.execute(
        """SELECT cs.phone, l.name, cs.stage, cs.raw_reason
           FROM contact_stages cs
           LEFT JOIN leads l ON cs.phone = l.phone
           WHERE cs.stage IN ('opted_out', 'demo_requested', 'interested', 'replied')
           ORDER BY cs.classified_at DESC LIMIT 10"""
    ).fetchall()

    conn.close()

    # Build message
    lines = []
    lines.append(f"*WhatsApp Campaign Report -- {report_date}*")
    lines.append("")
    lines.append(f"*Sent today:* {sent_today} (batch ~{batch_num})")
    if failed_today:
        lines.append(f"*Failed today:* {failed_today}")
    lines.append(f"*Replies (all time):* {total_replied} unique contacts")
    lines.append("")
    lines.append(f"*Pipeline:* {leads_remaining} leads remaining / {total_leads} total")
    lines.append(f"*Total sent (all time):* {total_sent}")
    lines.append("")

    # Breakdown by stage
    demo_entries = [r for r in recent if r["stage"] == "demo_requested"]
    interested_entries = [r for r in recent if r["stage"] == "interested"]
    optout_entries = [r for r in recent if r["stage"] == "opted_out"]
    other_entries = [r for r in recent if r["stage"] == "replied"]

    if demo_entries:
        lines.append(f"*Demo requests ({demo_requests}):*")
        for e in demo_entries[:5]:
            snippet = (e["raw_reason"] or "")[:80].replace("*", "").replace("_", "")
            lines.append(f"  - {e['name'] or e['phone']}: _{snippet}_")
        lines.append("")

    if interested_entries:
        lines.append(f"*Interested ({interested_count}):*")
        for e in interested_entries[:5]:
            snippet = (e["raw_reason"] or "")[:80].replace("*", "").replace("_", "")
            lines.append(f"  - {e['name'] or e['phone']}: _{snippet}_")
        lines.append("")

    if optout_entries:
        lines.append(f"*Opt-outs ({opt_outs}):*")
        for e in optout_entries[:5]:
            lines.append(f"  - {e['name'] or e['phone']}")
        lines.append("")

    if other_entries:
        lines.append(f"*Other replies:* {len(other_entries)}")
        lines.append("")

    if not recent:
        lines.append("_No classified replies yet._")

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
