"""SQLite database helper for WhatsApp pipeline data."""

import os
import sqlite3

DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "data", "whatsapp.db")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT UNIQUE NOT NULL,
    name TEXT,
    email TEXT,
    agency TEXT,
    city TEXT,
    region TEXT,
    active_listings INTEGER DEFAULT 0,
    source TEXT,
    url TEXT,
    whatsapp_link TEXT,
    status TEXT DEFAULT 'new',
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS outreach_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL,
    template_name TEXT,
    template_sid TEXT,
    twilio_sid TEXT UNIQUE,
    status TEXT NOT NULL,
    error TEXT,
    sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbound_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL,
    body TEXT,
    twilio_sid TEXT UNIQUE,
    status TEXT DEFAULT 'received',
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contact_stages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT UNIQUE NOT NULL,
    stage TEXT NOT NULL DEFAULT 'cold',
    classified_at TEXT DEFAULT (datetime('now')),
    classified_by TEXT DEFAULT 'keyword',
    confidence REAL,
    raw_reason TEXT,
    warmth_score INTEGER,
    timing_score INTEGER,
    relevance_score INTEGER,
    trust_score INTEGER,
    conversion_readiness_score INTEGER,
    quality_improvement_suggestion TEXT
);

CREATE TABLE IF NOT EXISTS quality_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL,
    warmth INTEGER NOT NULL CHECK(warmth BETWEEN 1 AND 5),
    timing INTEGER NOT NULL CHECK(timing BETWEEN 1 AND 5),
    relevance INTEGER NOT NULL CHECK(relevance BETWEEN 1 AND 5),
    trust INTEGER NOT NULL CHECK(trust BETWEEN 1 AND 5),
    conversion_readiness INTEGER NOT NULL CHECK(conversion_readiness BETWEEN 1 AND 5),
    proposed_change TEXT,
    scored_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_outreach_phone ON outreach_messages(phone);
CREATE INDEX IF NOT EXISTS idx_outreach_twilio_sid ON outreach_messages(twilio_sid);
CREATE INDEX IF NOT EXISTS idx_inbound_phone ON inbound_messages(phone);
CREATE INDEX IF NOT EXISTS idx_inbound_twilio_sid ON inbound_messages(twilio_sid);
CREATE INDEX IF NOT EXISTS idx_contact_stages_stage ON contact_stages(stage);
CREATE INDEX IF NOT EXISTS idx_quality_scores_phone ON quality_scores(phone);
"""

CONTACT_STAGE_OPTIONAL_COLUMNS = {
    "warmth_score": "INTEGER",
    "timing_score": "INTEGER",
    "relevance_score": "INTEGER",
    "trust_score": "INTEGER",
    "conversion_readiness_score": "INTEGER",
    "quality_improvement_suggestion": "TEXT",
}


def get_db(db_path=None):
    """Return a sqlite3 connection with row_factory set."""
    path = db_path or DEFAULT_DB_PATH
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    init_db(db)
    return db


def init_db(db):
    """Create all tables if they don't exist."""
    db.executescript(SCHEMA_SQL)
    ensure_contact_stages_schema(db)


def ensure_contact_stages_schema(db):
    """Backfill contact_stages columns for databases created before quality scoring."""
    existing = {
        row["name"]
        for row in db.execute("PRAGMA table_info(contact_stages)").fetchall()
    }
    for column_name, column_type in CONTACT_STAGE_OPTIONAL_COLUMNS.items():
        if column_name not in existing:
            db.execute(
                f"ALTER TABLE contact_stages ADD COLUMN {column_name} {column_type}"
            )


def add_lead(db, phone, name=None, email=None, agency=None, city=None,
             region=None, active_listings=0, source=None, url=None,
             whatsapp_link=None, status="new", notes=None):
    """Insert or ignore a lead."""
    db.execute(
        """INSERT OR IGNORE INTO leads
           (phone, name, email, agency, city, region, active_listings, source, url, whatsapp_link, status, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (phone, name, email, agency, city, region, active_listings, source, url, whatsapp_link, status, notes),
    )


def add_outreach_message(db, phone, template_name, template_sid, twilio_sid, status, error, sent_at):
    """Insert an outreach message. Skips duplicates by twilio_sid."""
    db.execute(
        """INSERT OR IGNORE INTO outreach_messages
           (phone, template_name, template_sid, twilio_sid, status, error, sent_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (phone, template_name, template_sid, twilio_sid, status, error or None, sent_at),
    )


def add_inbound_message(db, phone, body, twilio_sid, received_at, status="received"):
    """Insert an inbound message. Skips duplicates by twilio_sid."""
    db.execute(
        """INSERT OR IGNORE INTO inbound_messages
           (phone, body, twilio_sid, status, received_at)
           VALUES (?, ?, ?, ?, ?)""",
        (phone, body, twilio_sid, status, received_at),
    )


def get_lead_by_phone(db, phone):
    """Return a lead row by phone or None."""
    return db.execute("SELECT * FROM leads WHERE phone = ?", (phone,)).fetchone()


def update_contact_stage(
    db,
    phone,
    stage,
    classified_by="keyword",
    confidence=None,
    raw_reason=None,
    quality_scores=None,
    quality_improvement_suggestion=None,
):
    """Upsert the contact stage for a phone."""
    quality_scores = quality_scores or {}
    overwrite_quality = 1 if quality_scores else 0
    db.execute(
        """INSERT INTO contact_stages (
               phone,
               stage,
               classified_by,
               confidence,
               raw_reason,
               warmth_score,
               timing_score,
               relevance_score,
               trust_score,
               conversion_readiness_score,
               quality_improvement_suggestion,
               classified_at
           )
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
           ON CONFLICT(phone) DO UPDATE SET
             stage = excluded.stage,
             classified_by = excluded.classified_by,
             confidence = excluded.confidence,
             raw_reason = excluded.raw_reason,
             warmth_score = CASE
                 WHEN ? THEN excluded.warmth_score
                 ELSE contact_stages.warmth_score
             END,
             timing_score = CASE
                 WHEN ? THEN excluded.timing_score
                 ELSE contact_stages.timing_score
             END,
             relevance_score = CASE
                 WHEN ? THEN excluded.relevance_score
                 ELSE contact_stages.relevance_score
             END,
             trust_score = CASE
                 WHEN ? THEN excluded.trust_score
                 ELSE contact_stages.trust_score
             END,
             conversion_readiness_score = CASE
                 WHEN ? THEN excluded.conversion_readiness_score
                 ELSE contact_stages.conversion_readiness_score
             END,
             quality_improvement_suggestion = CASE
                 WHEN ? THEN excluded.quality_improvement_suggestion
                 ELSE contact_stages.quality_improvement_suggestion
             END,
             classified_at = excluded.classified_at""",
        (
            phone,
            stage,
            classified_by,
            confidence,
            raw_reason,
            quality_scores.get("warmth"),
            quality_scores.get("timing"),
            quality_scores.get("relevance"),
            quality_scores.get("trust"),
            quality_scores.get("conversion_readiness"),
            quality_improvement_suggestion,
            overwrite_quality,
            overwrite_quality,
            overwrite_quality,
            overwrite_quality,
            overwrite_quality,
            overwrite_quality,
        ),
    )


def get_contact_stage(db, phone):
    """Return the contact stage row for a phone or None."""
    return db.execute("SELECT * FROM contact_stages WHERE phone = ?", (phone,)).fetchone()


def add_quality_score(
    db,
    phone,
    warmth,
    timing,
    relevance,
    trust,
    conversion_readiness,
    proposed_change=None,
):
    """Insert a quality scoring snapshot for a phone."""
    db.execute(
        """INSERT INTO quality_scores (
               phone,
               warmth,
               timing,
               relevance,
               trust,
               conversion_readiness,
               proposed_change
           )
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            phone,
            warmth,
            timing,
            relevance,
            trust,
            conversion_readiness,
            proposed_change,
        ),
    )


def get_latest_quality_score(db, phone):
    """Return the most recent quality score row for a phone or None."""
    return db.execute(
        """SELECT *
           FROM quality_scores
           WHERE phone = ?
           ORDER BY datetime(scored_at) DESC, id DESC
           LIMIT 1""",
        (phone,),
    ).fetchone()


def get_pipeline_stats(db):
    """Return a dict with counts per pipeline stage."""
    rows = db.execute(
        "SELECT stage, COUNT(*) as cnt FROM contact_stages GROUP BY stage"
    ).fetchall()
    stats = {r["stage"]: r["cnt"] for r in rows}

    total_leads = db.execute("SELECT COUNT(*) as cnt FROM leads WHERE phone IS NOT NULL AND phone != ''").fetchone()["cnt"]
    total_contacted = db.execute("SELECT COUNT(DISTINCT phone) as cnt FROM outreach_messages WHERE status = 'sent'").fetchone()["cnt"]
    total_replied = db.execute(
        """SELECT COUNT(DISTINCT im.phone) as cnt FROM inbound_messages im
           INNER JOIN outreach_messages om ON im.phone = om.phone
           WHERE om.status = 'sent'"""
    ).fetchone()["cnt"]

    return {
        "total_leads": total_leads,
        "contacted": total_contacted,
        "replied": total_replied,
        "stages": stats,
    }


def get_conversations(db):
    """Return list of conversations with latest message time and contact info."""
    rows = db.execute(
        """SELECT
             m.phone,
             l.name,
             l.agency,
             cs.stage,
             MAX(m.ts) as last_message_at,
             COUNT(*) as message_count
           FROM (
             SELECT phone, sent_at as ts FROM outreach_messages WHERE status = 'sent'
             UNION ALL
             SELECT phone, received_at as ts FROM inbound_messages
           ) m
           LEFT JOIN leads l ON m.phone = l.phone
           LEFT JOIN contact_stages cs ON m.phone = cs.phone
           GROUP BY m.phone
           ORDER BY last_message_at DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def get_messages_for_phone(db, phone):
    """Return all messages for a phone, ordered by time."""
    rows = db.execute(
        """SELECT phone, 'outbound' as direction, template_name as body, sent_at as timestamp, twilio_sid
           FROM outreach_messages WHERE phone = ? AND status = 'sent'
           UNION ALL
           SELECT phone, 'inbound' as direction, body, received_at as timestamp, twilio_sid
           FROM inbound_messages WHERE phone = ?
           ORDER BY timestamp ASC""",
        (phone, phone),
    ).fetchall()
    return [dict(r) for r in rows]


def get_daily_stats(db, days=30):
    """Return daily sent/failed counts for the last N days."""
    rows = db.execute(
        """SELECT DATE(sent_at) as day,
                  SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) as sent,
                  SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed
           FROM outreach_messages
           WHERE sent_at >= datetime('now', ?)
           GROUP BY DATE(sent_at)
           ORDER BY day DESC""",
        (f"-{days} days",),
    ).fetchall()
    return [dict(r) for r in rows]


def phone_already_sent(db, phone, template_name):
    """Check if a message was already sent to this phone with this template."""
    row = db.execute(
        "SELECT 1 FROM outreach_messages WHERE phone = ? AND template_name = ? AND status = 'sent' LIMIT 1",
        (phone, template_name),
    ).fetchone()
    return row is not None
