#!/usr/bin/env python3
"""
WhatsApp Conversation Viewer + Outreach Dashboard
Flask app to view WhatsApp conversations and track outreach metrics.
Runs on port 5050.
"""

import os
import sys
from datetime import datetime, date, timezone
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request, abort

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

# Add parent dir so we can import db module
sys.path.insert(0, str(Path(__file__).parent.parent))
import db as whatsapp_db

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

# Init Twilio client using API key auth
from twilio.rest import Client
client = Client(TWILIO_API_KEY_SID, TWILIO_API_KEY_SECRET, account_sid=TWILIO_ACCOUNT_SID)

app = Flask(__name__)
app.json.sort_keys = False


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

    # Enrich with stage info from SQLite
    conn = whatsapp_db.get_db()
    stages = {}
    for row in conn.execute("SELECT phone, stage FROM contact_stages").fetchall():
        stages[row["phone"]] = row["stage"]
    conn.close()

    stage_filter = request.args.get("stage", "")

    result = {}
    for phone, messages in convos.items():
        last_time = messages[-1]["time"] if messages else ""
        phone_stage = stages.get(phone, "cold")
        has_inbound = any(m["direction"] == "received" for m in messages)

        if stage_filter and stage_filter != "all":
            if stage_filter == "has_reply":
                if not has_inbound:
                    continue
            elif phone_stage != stage_filter:
                continue

        result[phone] = {
            "messages": messages,
            "last_message_time": last_time,
            "stage": phone_stage,
            "has_reply": has_inbound,
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

    conn = whatsapp_db.get_db()
    stats = whatsapp_db.get_pipeline_stats(conn)
    stages = stats["stages"]

    total_leads = stats["total_leads"]
    total_sent = stats["contacted"]
    total_replied = stats["replied"]
    reply_rate = round(total_replied / total_sent * 100, 1) if total_sent else 0

    opt_outs = stages.get("opted_out", 0)
    interested = stages.get("interested", 0)
    demo_requests = stages.get("demo_requested", 0)

    # Daily breakdown from SQLite
    daily_list = whatsapp_db.get_daily_stats(conn, days=30)

    today_str = date.today().isoformat()
    today_sent = 0
    for d in daily_list:
        if d["day"] == today_str:
            today_sent = d["sent"]
            break

    # Format daily for API compatibility
    daily_formatted = [{"date": d["day"], "sent": d["sent"], "failed": d["failed"]} for d in daily_list]

    # Recent replies (last 10 inbound from contacted phones)
    recent = conn.execute(
        """SELECT im.phone, l.name, im.body, im.received_at as timestamp, cs.stage
           FROM inbound_messages im
           LEFT JOIN leads l ON im.phone = l.phone
           LEFT JOIN contact_stages cs ON im.phone = cs.phone
           INNER JOIN outreach_messages om ON im.phone = om.phone AND om.status = 'sent'
           ORDER BY im.received_at DESC
           LIMIT 10"""
    ).fetchall()
    recent_replies = [
        {"phone": r["phone"], "name": r["name"] or r["phone"],
         "body": (r["body"] or "")[:120], "timestamp": r["timestamp"] or "",
         "stage": r["stage"] or ""}
        for r in recent
    ]

    conn.close()

    return jsonify({
        "summary": {
            "total_leads": total_leads,
            "leads_remaining": total_leads - total_sent,
            "total_sent": total_sent,
            "today_sent": today_sent,
            "total_replied": total_replied,
            "reply_rate": reply_rate,
            "opt_outs": opt_outs,
            "interested": interested + demo_requests,
            "demo_requests": demo_requests,
        },
        "pipeline": {
            "leads": total_leads,
            "contacted": total_sent,
            "replied": total_replied,
            "interested": interested + demo_requests,
            "demo": demo_requests,
        },
        "daily": daily_formatted[:30],
        "recent_replies": recent_replies,
    })


if __name__ == "__main__":
    app.run(port=5050, debug=False)
