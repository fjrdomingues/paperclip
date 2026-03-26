#!/usr/bin/env python3
"""
WhatsApp Conversation Viewer + Outreach Dashboard
Flask app to view WhatsApp conversations and track outreach metrics.
Runs on port 5050.
"""

import csv
import json
import os
from collections import defaultdict
from datetime import datetime, date, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, abort

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

# Load env from shared telegram .env
dotenv_path = os.path.join(os.path.dirname(__file__), "..", "..", "telegram", ".env")
load_dotenv(dotenv_path)

TWILIO_ACCOUNT_SID = os.environ["TWILIO_ACCOUNT_SID"]
TWILIO_API_KEY_SID = os.environ["TWILIO_API_KEY_SID"]
TWILIO_API_KEY_SECRET = os.environ["TWILIO_API_KEY_SECRET"]
WHATSAPP_FROM = os.environ["TWILIO_WHATSAPP_FROM"]  # e.g. whatsapp:+351912508220
VIEWER_PASSWORD = os.environ.get("VIEWER_PASSWORD", "")

LISBON_TZ = ZoneInfo("Europe/Lisbon")

SCRIPT_DIR = Path(__file__).parent
WHATSAPP_DIR = SCRIPT_DIR.parent
PROJECT_ROOT = WHATSAPP_DIR.parent.parent
SENT_LOG_FILE = WHATSAPP_DIR / "data" / "sent_log.csv"
INBOX_FILE = WHATSAPP_DIR / "data" / "inbox.jsonl"
LEADS_FILE = PROJECT_ROOT / "projects" / "growth" / "data" / "leads.csv"

OPT_OUT_KEYWORDS = ["stop", "parar", "remover", "cancelar", "não quero", "nao quero",
                    "não tenho interesse", "nao tenho interesse", "não me contacte",
                    "nao me contacte", "descadastrar", "sair", "unsubscribe"]
INTERESTED_KEYWORDS = ["interesse", "interessado", "interessada", "sim", "quero saber",
                       "mais informação", "mais informacao", "quero", "pode enviar",
                       "como funciona", "fale mais", "diz-me mais", "conta-me mais"]
DEMO_KEYWORDS = ["reunião", "reuniao", "ligar", "chamada", "call", "demo", "agendar",
                 "marcar", "disponível", "disponivel", "quando podemos", "meet"]


def classify_reply(body: str) -> str:
    text = body.lower()
    if any(k in text for k in OPT_OUT_KEYWORDS):
        return "opt_out"
    if any(k in text for k in DEMO_KEYWORDS):
        return "demo"
    if any(k in text for k in INTERESTED_KEYWORDS):
        return "interested"
    return "neutral"

# Init Twilio client using API key auth
from twilio.rest import Client
client = Client(TWILIO_API_KEY_SID, TWILIO_API_KEY_SECRET, account_sid=TWILIO_ACCOUNT_SID)

app = Flask(__name__)


def format_time(iso_str):
    """Convert ISO time string to Lisbon timezone display."""
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_lisbon = dt.astimezone(LISBON_TZ)
        return dt_lisbon.strftime("%d/%m %H:%M")
    except Exception:
        return iso_str


def get_conversations():
    """Fetch all messages from Twilio and group by phone number."""
    try:
        sent = client.messages.list(from_=WHATSAPP_FROM, limit=500)
        received = client.messages.list(to=WHATSAPP_FROM, limit=500)
    except Exception as e:
        app.logger.error(f"Twilio API error: {e}")
        return {}

    all_msgs = []
    for m in sent:
        if not m.to or not m.to.startswith("whatsapp:"):
            continue
        phone = m.to.replace("whatsapp:", "")
        date_sent = m.date_sent.isoformat() if m.date_sent else ""
        all_msgs.append({
            "phone": phone,
            "body": m.body or "",
            "time": date_sent,
            "direction": "sent",
            "status": m.status,
        })
    for m in received:
        if not m.from_ or not m.from_.startswith("whatsapp:"):
            continue
        phone = m.from_.replace("whatsapp:", "")
        date_sent = m.date_sent.isoformat() if m.date_sent else ""
        all_msgs.append({
            "phone": phone,
            "body": m.body or "",
            "time": date_sent,
            "direction": "received",
            "status": m.status,
        })

    convos = {}
    for msg in all_msgs:
        phone = msg["phone"]
        if phone not in convos:
            convos[phone] = []
        convos[phone].append(msg)

    for phone in convos:
        convos[phone].sort(key=lambda x: x["time"])

    return convos


def check_password():
    if VIEWER_PASSWORD and request.args.get("pw") != VIEWER_PASSWORD:
        abort(401)


@app.route("/")
def index():
    check_password()
    pw_param = f"?pw={VIEWER_PASSWORD}" if VIEWER_PASSWORD else ""
    return render_template("index.html", pw_param=pw_param)


@app.route("/api/conversations")
def api_conversations():
    convos = get_conversations()
    result = {}
    for phone, messages in convos.items():
        last_time = messages[-1]["time"] if messages else ""
        result[phone] = {
            "messages": messages,
            "last_message_time": last_time,
        }
    # Sort by most recent first
    sorted_result = dict(
        sorted(result.items(), key=lambda x: x[1]["last_message_time"], reverse=True)
    )
    return jsonify(sorted_result)


@app.route("/api/conversations/<path:phone>")
def api_conversation(phone):
    convos = get_conversations()
    messages = convos.get(phone, [])
    return jsonify(messages)


@app.route("/api/send", methods=["POST"])
def api_send():
    data = request.get_json()
    if not data or not data.get("to") or not data.get("body"):
        return jsonify({"error": "Missing 'to' or 'body'"}), 400
    to = data["to"]
    body = data["body"]
    try:
        msg = client.messages.create(
            from_=WHATSAPP_FROM,
            to=f"whatsapp:{to}",
            body=body,
        )
        return jsonify({"sid": msg.sid, "status": msg.status})
    except Exception as e:
        app.logger.error(f"Send error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/dashboard")
def dashboard():
    check_password()
    pw_param = f"?pw={VIEWER_PASSWORD}" if VIEWER_PASSWORD else ""
    return render_template("dashboard.html", pw_param=pw_param)


@app.route("/api/dashboard")
def api_dashboard():
    check_password()

    # Load sent log
    sent_rows = []
    if SENT_LOG_FILE.exists():
        with open(SENT_LOG_FILE, newline="") as f:
            sent_rows = list(csv.DictReader(f))

    # Load inbox
    inbox = []
    if INBOX_FILE.exists():
        with open(INBOX_FILE) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        inbox.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass

    # Load leads
    leads = []
    if LEADS_FILE.exists():
        with open(LEADS_FILE, newline="") as f:
            leads = list(csv.DictReader(f))

    # --- Sent stats ---
    sent_by_phone = defaultdict(list)
    for row in sent_rows:
        sent_by_phone[row["phone"]].append(row)

    all_sent_phones = {phone for phone, rows in sent_by_phone.items()
                       if any(r.get("status") == "sent" for r in rows)}

    # Daily breakdown
    daily = defaultdict(lambda: {"sent": 0, "failed": 0})
    for row in sent_rows:
        day = row.get("sent_at", "")[:10]
        if not day:
            continue
        if row.get("status") == "sent":
            daily[day]["sent"] += 1
        else:
            daily[day]["failed"] += 1

    daily_list = sorted(
        [{"date": d, **v} for d, v in daily.items()],
        key=lambda x: x["date"],
        reverse=True,
    )

    # --- Inbox: classify replies ---
    replied_phones = defaultdict(list)
    for msg in inbox:
        phone = msg.get("from", "").strip()
        if not phone.startswith("+"):
            phone = "+" + phone.lstrip("0")
        replied_phones[phone].append(msg)

    opt_outs, demo_requests, interested_list, neutral_list = [], [], [], []
    replied_from_contacted = set()

    for phone, msgs in replied_phones.items():
        if phone not in all_sent_phones:
            continue
        replied_from_contacted.add(phone)
        name = next(
            (r["name"] for r in sent_by_phone.get(phone, []) if r.get("name")),
            phone,
        )
        for msg in msgs:
            body = msg.get("body", "")
            cls = classify_reply(body)
            entry = {
                "phone": phone,
                "name": name,
                "body": body[:120],
                "timestamp": msg.get("timestamp", ""),
            }
            if cls == "opt_out":
                opt_outs.append(entry)
            elif cls == "demo":
                demo_requests.append(entry)
            elif cls == "interested":
                interested_list.append(entry)
            else:
                neutral_list.append(entry)

    # Leads remaining (not yet contacted)
    leads_with_phone = [l for l in leads if l.get("phone", "").strip()]
    remaining = [l for l in leads_with_phone
                 if l.get("phone", "").strip() not in all_sent_phones]

    total_sent = len(all_sent_phones)
    total_replied = len(replied_from_contacted)
    reply_rate = round(total_replied / total_sent * 100, 1) if total_sent else 0
    interest_count = len({e["phone"] for e in demo_requests + interested_list})

    # Pipeline stages
    pipeline = {
        "leads": len(leads_with_phone),
        "contacted": total_sent,
        "replied": total_replied,
        "interested": interest_count,
        "demo": len({e["phone"] for e in demo_requests}),
    }

    # Recent replies (last 10, sorted newest first)
    all_replies = demo_requests + interested_list + neutral_list + opt_outs
    all_replies.sort(key=lambda x: x["timestamp"], reverse=True)
    recent_replies = all_replies[:10]

    today = date.today().isoformat()
    today_sent = daily.get(today, {}).get("sent", 0)

    return jsonify({
        "summary": {
            "total_leads": len(leads_with_phone),
            "leads_remaining": len(remaining),
            "total_sent": total_sent,
            "today_sent": today_sent,
            "total_replied": total_replied,
            "reply_rate": reply_rate,
            "opt_outs": len(opt_outs),
            "interested": interest_count,
            "demo_requests": len({e["phone"] for e in demo_requests}),
        },
        "pipeline": pipeline,
        "daily": daily_list[:30],
        "recent_replies": recent_replies,
    })


if __name__ == "__main__":
    app.run(port=5050, debug=False)
