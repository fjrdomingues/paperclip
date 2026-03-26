#!/usr/bin/env python3
"""
WhatsApp Conversation Viewer
Flask app to view and reply to WhatsApp conversations via Twilio.
Runs on port 5050.
"""

import os
from datetime import datetime, timezone

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


if __name__ == "__main__":
    app.run(port=5050, debug=False)
