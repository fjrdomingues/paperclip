#!/usr/bin/env python3
"""Wrapper to run poll-inbox.sh from LaunchAgent, then sync new messages to SQLite."""
import json
import os
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
DATA_DIR = os.environ.get("WHATSAPP_POLL_DATA_DIR", os.path.join(SCRIPT_DIR, "data"))
INBOX_FILE = os.environ.get("WHATSAPP_POLL_INBOX_FILE", os.path.join(DATA_DIR, "inbox.jsonl"))
DB_PATH = os.environ.get("WHATSAPP_DB_PATH", os.path.join(DATA_DIR, "whatsapp.db"))

script = os.path.join(SCRIPT_DIR, "poll-inbox.sh")
subprocess.run(["/bin/bash", script], check=False)

# Sync inbox.jsonl → SQLite (idempotent via UNIQUE twilio_sid)
import db as whatsapp_db

if os.path.exists(INBOX_FILE):
    conn = whatsapp_db.get_db(DB_PATH)
    whatsapp_db.init_db(conn)
    count = 0
    with open(INBOX_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            phone = msg.get("from", "").strip()
            if phone and not phone.startswith("+"):
                phone = "+" + phone
            if not phone:
                continue
            whatsapp_db.add_inbound_message(
                conn,
                phone=phone,
                body=msg.get("body", ""),
                twilio_sid=msg.get("sid"),
                received_at=msg.get("timestamp"),
            )
            count += 1
    conn.commit()
    conn.close()
