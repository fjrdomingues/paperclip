#!/usr/bin/env python3
"""Migrate WhatsApp pipeline data from flat files to SQLite.

Reads:
  - projects/growth/data/leads.csv
  - projects/whatsapp/data/sent_log.csv
  - projects/whatsapp/data/inbox.jsonl

Writes:
  - projects/whatsapp/data/whatsapp.db

Idempotent: safe to re-run (uses INSERT OR IGNORE).
"""

import csv
import json
import os
import sys

# Add project root to path so we can import db module
sys.path.insert(0, os.path.dirname(__file__))
import db as whatsapp_db

BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")
GROWTH_DIR = os.path.join(BASE_DIR, "..", "growth", "data")

LEADS_CSV = os.path.join(GROWTH_DIR, "leads.csv")
SENT_LOG_CSV = os.path.join(DATA_DIR, "sent_log.csv")
INBOX_JSONL = os.path.join(DATA_DIR, "inbox.jsonl")

# Keyword sets from followup_engine.py for initial stage classification
OPTOUT_KEYWORDS = [
    "stop", "parar", "cancelar", "remover", "sair", "não quero", "nao quero",
    "desinscrever", "desinscrição", "não me contacte", "nao me contacte",
    "remova", "deixe de", "não estou interessado", "nao estou interessado",
]
INTEREST_KEYWORDS = [
    "sim", "quero", "interesse", "interessado", "interessada",
    "mais informação", "mais informacao", "como funciona", "preço", "preco",
    "quanto custa", "quanto é", "quanto e", "demo", "experimentar",
    "saber mais", "ver mais", "ok", "claro", "com certeza", "pode ser",
    "gostaria",
]
DEMO_KEYWORDS = [
    "reunião", "reuniao", "ligar", "chamada", "call", "demo", "agendar",
    "marcar", "disponível", "disponivel", "quando podemos", "meet",
]


def classify_reply(body):
    """Classify a reply body into a pipeline stage using keyword matching."""
    if not body:
        return "replied"
    lower = body.lower().strip()
    if not lower:
        return "replied"
    for kw in OPTOUT_KEYWORDS:
        if kw in lower:
            return "opted_out"
    for kw in DEMO_KEYWORDS:
        if kw in lower:
            return "demo_requested"
    for kw in INTEREST_KEYWORDS:
        if kw in lower:
            return "interested"
    return "replied"


def normalize_phone(phone):
    """Normalize phone: strip whitespace, ensure + prefix."""
    if not phone:
        return phone
    phone = phone.strip()
    if phone and not phone.startswith("+"):
        phone = "+" + phone
    return phone


def migrate_leads(conn):
    """Import leads from CSV."""
    if not os.path.exists(LEADS_CSV):
        print(f"  SKIP: {LEADS_CSV} not found")
        return 0
    count = 0
    with open(LEADS_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            phone = normalize_phone(row.get("phone", ""))
            if not phone:
                continue
            whatsapp_db.add_lead(
                conn,
                phone=phone,
                name=row.get("name"),
                email=row.get("email"),
                agency=row.get("agency"),
                city=row.get("city"),
                region=row.get("region"),
                active_listings=int(row.get("active_listings") or 0),
                source=row.get("source"),
                url=row.get("url"),
                whatsapp_link=row.get("whatsapp"),
                status=row.get("status", "new"),
                notes=row.get("notes"),
            )
            count += 1
    conn.commit()
    return count


def migrate_sent_log(conn):
    """Import sent log from CSV."""
    if not os.path.exists(SENT_LOG_CSV):
        print(f"  SKIP: {SENT_LOG_CSV} not found")
        return 0
    count = 0
    with open(SENT_LOG_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            phone = normalize_phone(row.get("phone", ""))
            if not phone:
                continue
            whatsapp_db.add_outreach_message(
                conn,
                phone=phone,
                template_name=row.get("template_name"),
                template_sid=row.get("template_sid"),
                twilio_sid=row.get("twilio_sid"),
                status=row.get("status", "sent"),
                error=row.get("error"),
                sent_at=row.get("sent_at"),
            )
            count += 1
    conn.commit()
    return count


def migrate_inbox(conn):
    """Import inbox messages from JSONL."""
    if not os.path.exists(INBOX_JSONL):
        print(f"  SKIP: {INBOX_JSONL} not found")
        return 0
    count = 0
    with open(INBOX_JSONL, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            phone = normalize_phone(msg.get("from", ""))
            if not phone:
                continue
            whatsapp_db.add_inbound_message(
                conn,
                phone=phone,
                body=msg.get("body", ""),
                twilio_sid=msg.get("sid"),
                received_at=msg.get("timestamp"),
                status=msg.get("status", "received"),
            )
            count += 1
    conn.commit()
    return count


def populate_contact_stages(conn):
    """Derive contact stages from existing data."""
    # All leads with phones start as 'cold'
    leads = conn.execute("SELECT phone FROM leads WHERE phone IS NOT NULL AND phone != ''").fetchall()
    for lead in leads:
        whatsapp_db.update_contact_stage(conn, lead["phone"], "cold", "migration")

    # Contacted: phones with sent outreach
    contacted = conn.execute(
        "SELECT DISTINCT phone FROM outreach_messages WHERE status = 'sent'"
    ).fetchall()
    for row in contacted:
        whatsapp_db.update_contact_stage(conn, row["phone"], "contacted", "migration")

    # Replied: classify based on inbound message content
    replied = conn.execute(
        """SELECT im.phone, im.body FROM inbound_messages im
           INNER JOIN outreach_messages om ON im.phone = om.phone
           WHERE om.status = 'sent'
           ORDER BY im.received_at ASC"""
    ).fetchall()

    # Track best stage per phone (most advanced wins)
    stage_priority = {
        "cold": 0, "contacted": 1, "replied": 2, "auto_responder": 3,
        "interested": 4, "demo_requested": 5, "opted_out": 6,
    }
    phone_stages = {}
    for row in replied:
        phone = row["phone"]
        stage = classify_reply(row["body"])
        if stage_priority.get(stage, 0) > stage_priority.get(phone_stages.get(phone, ""), -1):
            phone_stages[phone] = stage

    for phone, stage in phone_stages.items():
        whatsapp_db.update_contact_stage(conn, phone, stage, "keyword")

    conn.commit()
    return len(phone_stages)


def main():
    print("=== WhatsApp SQLite Migration ===\n")

    conn = whatsapp_db.get_db()
    whatsapp_db.init_db(conn)

    print("1. Migrating leads...")
    n = migrate_leads(conn)
    print(f"   → {n} leads processed")

    print("2. Migrating sent log...")
    n = migrate_sent_log(conn)
    print(f"   → {n} outreach messages processed")

    print("3. Migrating inbox...")
    n = migrate_inbox(conn)
    print(f"   → {n} inbound messages processed")

    print("4. Populating contact stages...")
    n = populate_contact_stages(conn)
    print(f"   → {n} phone stages classified from replies")

    # Summary
    print("\n=== Verification ===")
    stats = whatsapp_db.get_pipeline_stats(conn)
    print(f"Total leads:    {stats['total_leads']}")
    print(f"Contacted:      {stats['contacted']}")
    print(f"Replied:        {stats['replied']}")
    print(f"Stages:         {dict(stats['stages'])}")

    conn.close()
    print("\n✓ Migration complete. DB at:", whatsapp_db.DEFAULT_DB_PATH)


if __name__ == "__main__":
    main()
