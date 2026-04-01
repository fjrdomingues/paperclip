#!/usr/bin/env python3
"""
WhatsApp Outreach Sender
Reads leads from projects/growth/data/leads.csv, sends initial outreach templates,
logs results to projects/whatsapp/data/sent_log.csv.

Usage:
  python outreach_sender.py [--template remodelar_initial_outreach] [--daily-cap 30] [--dry-run]
  python outreach_sender.py --one-shot [--template ...] [--daily-cap 50]

  --one-shot: Send at most 1 message per invocation, respecting a 2-5 min random
              interval tracked in data/sender_state.json. Designed for cron use
              (run every 60s; actual sends happen every 2-5 min via state file).

Env vars (loaded from projects/telegram/.env if present):
  TWILIO_ACCOUNT_SID, TWILIO_API_KEY_SID, TWILIO_API_KEY_SECRET, TWILIO_WHATSAPP_FROM
  PAPERCLIP_CEO_API_KEY  (optional) used to post Paperclip alert on rate-limit errors
  PAPERCLIP_API_URL      (optional, default http://localhost:3100)
"""

import argparse
import csv
import json
import os
import random
import sys
import time
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
import ssl
from urllib import request, parse
from urllib.error import HTTPError

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore

import db as whatsapp_db

LISBON_TZ = ZoneInfo("Europe/Lisbon")
BUSINESS_HOURS_START = 9   # 09:00
BUSINESS_HOURS_END = 18    # 18:00

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
LEADS_FILE = PROJECT_ROOT / "projects" / "growth" / "data" / "leads.csv"
TEMPLATES_FILE = SCRIPT_DIR / "templates.json"
SENT_LOG_FILE = SCRIPT_DIR / "data" / "sent_log.csv"
ENV_FILE = PROJECT_ROOT / "projects" / "telegram" / ".env"

RATE_LIMIT_DELAY = 1.5  # seconds between sends (batch mode)
ONE_SHOT_MIN_DELAY = 120   # seconds minimum between one-shot sends (2 min)
ONE_SHOT_MAX_DELAY = 300   # seconds maximum between one-shot sends (5 min)
SENDER_STATE_FILE = SCRIPT_DIR / "data" / "sender_state.json"

# Error codes that indicate Meta rate-limiting / spam blocking
RATE_LIMIT_ERROR_CODES = {63112, 63114, 63116, 21611}

LEGACY_SENT_LOG_FIELDS = [
    "phone",
    "name",
    "agency",
    "template_name",
    "template_sid",
    "sent_at",
    "twilio_sid",
    "status",
    "error",
]
SENT_LOG_FIELDS = [
    "phone",
    "name",
    "agency",
    "template_name",
    "template_sid",
    "variant",
    "sent_at",
    "twilio_sid",
    "status",
    "error",
]

# Conversation-first templates (A/B/C rotation) — WIN-314
CONVERSA_TEMPLATES = [
    ("remodelar_conversa_mercado", "A"),
    ("remodelar_conversa_elogio", "B"),
    ("remodelar_conversa_desafio", "C"),
]

TEMPLATE_VARIANT_MAP = {
    "remodelar_conversa_mercado": "A",
    "remodelar_conversa_elogio": "B",
    "remodelar_conversa_desafio": "C",
}

PAPERCLIP_API_URL = os.environ.get("PAPERCLIP_API_URL", "http://localhost:3100")
WIN75_ISSUE_ID = "117b8e31-b203-42e6-b57e-63651eca6a69"


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


def _empty_sent_log_entry():
    return {field: "" for field in SENT_LOG_FIELDS}


def _normalize_sent_log_entry(entry):
    normalized = _empty_sent_log_entry()
    for field in SENT_LOG_FIELDS:
        normalized[field] = (entry.get(field, "") or "").strip()
    return normalized


def _parse_sent_log_row(row, header_fields):
    if not row or not any((value or "").strip() for value in row):
        return None, False

    header_fields = [field.strip() for field in header_fields]
    header_is_current = header_fields == SENT_LOG_FIELDS
    normalized = _empty_sent_log_entry()
    needs_repair = not header_is_current

    if header_is_current:
        for field, value in zip(header_fields, row):
            normalized[field] = (value or "").strip()
        if len(row) < len(SENT_LOG_FIELDS):
            needs_repair = True
        elif len(row) > len(SENT_LOG_FIELDS):
            normalized["error"] = ",".join((value or "").strip() for value in row[len(SENT_LOG_FIELDS) - 1:])
            needs_repair = True
        return normalized, needs_repair

    if len(row) >= len(SENT_LOG_FIELDS):
        for field, value in zip(SENT_LOG_FIELDS, row):
            normalized[field] = (value or "").strip()
        if len(row) > len(SENT_LOG_FIELDS):
            normalized["error"] = ",".join((value or "").strip() for value in row[len(SENT_LOG_FIELDS) - 1:])
        return normalized, True

    for field, value in zip(LEGACY_SENT_LOG_FIELDS, row):
        normalized[field] = (value or "").strip()
    return normalized, True


def _read_sent_log_rows(sent_log_file=SENT_LOG_FILE):
    if not sent_log_file.exists():
        return [], False

    with open(sent_log_file, newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None:
            return [], False

        normalized_header = [field.strip() for field in header]
        entries = []
        needs_repair = normalized_header != SENT_LOG_FIELDS
        for row in reader:
            entry, row_needs_repair = _parse_sent_log_row(row, normalized_header)
            if entry is None:
                continue
            entries.append(entry)
            needs_repair = needs_repair or row_needs_repair
    return entries, needs_repair


def _rewrite_sent_log(entries, sent_log_file=SENT_LOG_FILE):
    sent_log_file.parent.mkdir(parents=True, exist_ok=True)
    tmp_file = sent_log_file.with_suffix(sent_log_file.suffix + ".tmp")
    with open(tmp_file, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SENT_LOG_FIELDS)
        writer.writeheader()
        for entry in entries:
            writer.writerow(_normalize_sent_log_entry(entry))
    tmp_file.replace(sent_log_file)


def ensure_sent_log_canonical(sent_log_file=SENT_LOG_FILE):
    entries, needs_repair = _read_sent_log_rows(sent_log_file)
    if sent_log_file.exists() and needs_repair:
        _rewrite_sent_log(entries, sent_log_file)
    return entries


def load_sent_log(sent_log_file=SENT_LOG_FILE):
    """Return sent-log entries keyed by phone, across legacy and current CSV schemas."""
    sent = {}
    entries, _needs_repair = _read_sent_log_rows(sent_log_file)
    for row in entries:
        phone = row.get("phone", "")
        if not phone:
            continue
        sent.setdefault(phone, []).append(row)
    return sent


def count_sent_today(sent_log, today=None):
    today = today or date.today().isoformat()
    count = 0
    for entries in sent_log.values():
        for e in entries:
            if e.get("sent_at", "").startswith(today) and e.get("status") == "sent":
                count += 1
    return count


def append_sent_log(entry, sent_log_file=SENT_LOG_FILE):
    entry = _normalize_sent_log_entry(entry)
    if sent_log_file.exists():
        ensure_sent_log_canonical(sent_log_file)
    sent_log_file.parent.mkdir(parents=True, exist_ok=True)
    file_exists = sent_log_file.exists()
    with open(sent_log_file, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=SENT_LOG_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(entry)


def is_business_hours():
    """Return True if current Lisbon time is within 09:00-18:00 WET/WEST."""
    now_lisbon = datetime.now(LISBON_TZ)
    return BUSINESS_HOURS_START <= now_lisbon.hour < BUSINESS_HOURS_END


def ensure_whatsapp_prefix(phone):
    if not phone.startswith("whatsapp:"):
        return f"whatsapp:{phone}"
    return phone


def send_template(account_sid, api_key_sid, api_key_secret, from_number, to_number, content_sid, variables):
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    vars_json = json.dumps(variables)
    data = parse.urlencode({
        "From": from_number,
        "To": ensure_whatsapp_prefix(to_number),
        "ContentSid": content_sid,
        "ContentVariables": vars_json,
    }).encode()
    req = request.Request(url, data=data, method="POST")
    import base64
    credentials = base64.b64encode(f"{api_key_sid}:{api_key_secret}".encode()).decode()
    req.add_header("Authorization", f"Basic {credentials}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with request.urlopen(req, context=_SSL_CTX) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        body = e.read().decode()
        return json.loads(body) if body else {"error_message": str(e), "status": "failed"}


def load_leads(leads_file=LEADS_FILE):
    if not Path(leads_file).exists():
        print(f"ERROR: Leads file not found: {leads_file}", file=sys.stderr)
        print("Run WIN-52 scraper first to populate leads.", file=sys.stderr)
        sys.exit(1)
    leads = []
    with open(leads_file, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            leads.append(row)
    return leads


def extract_first_name(full_name):
    """Extract first name for template variable."""
    parts = full_name.strip().split()
    return parts[0] if parts else full_name


# --- Segment-based template selection (WIN-250) ---

# Short agency names for template variable (long legal names → brand)
AGENCY_SHORT_NAMES = {
    "Prestígio Global - Sociedade de Mediação Imobiliária, S.A.": "RE/MAX Prestígio Global",
    "FCGM - Sociedade de Mediação Imobiliária, S.A.": "RE/MAX FCGM",
    "Sold Fast - Mediação Imobiliária, Lda": "RE/MAX Sold Fast",
    "Worldwidexl Lda": "RE/MAX Worldwide",
    "Sentir Lisboa - Mediação Imobiliária, Lda": "RE/MAX Sentir Lisboa",
    "Dinastia Infalível Lda.": "RE/MAX Dinastia",
    "EstorilHouse - Mediação Imobiliária, Lda": "RE/MAX EstorilHouse",
    "Vintage Patamar - Mediação Imobiliária, Lda": "RE/MAX Vintage Patamar",
    "Sold Now, Lda": "RE/MAX Sold Now",
    "Duplo Prestígio - Mediação Imobiliária, Lda": "RE/MAX Duplo Prestígio",
    "PartilhaNotável Mediação Imobiliária, Lda": "RE/MAX PartilhaNotável",
    "João Bordalo - Mediação Imob. Lda": "RE/MAX João Bordalo",
    "Maxloja - Mediação Imobiliária Lda": "RE/MAX Maxloja",
    "Números Pautados, Lda": "RE/MAX Números Pautados",
    "CENTURY 21": "Century 21",
}

LISBOA_REGION_CITIES = {
    "Lisboa", "Cascais", "Oeiras", "Sintra", "Loures", "Amadora",
    "Odivelas", "Vila Franca de Xira", "Almada", "Seixal", "Barreiro",
    "Setúbal", "Sesimbra", "Palmela", "Montijo", "Alcochete", "Moita",
}

PORTO_REGION_CITIES = {
    "Porto", "Vila Nova de Gaia", "Matosinhos", "Maia", "Gondomar",
    "Valongo", "Vila do Conde", "Póvoa de Varzim", "Braga", "Guimarães",
    "Espinho", "Santa Maria da Feira",
}


def get_agency_short_name(agency):
    """Return a short, recognizable agency name for template variable."""
    if not agency:
        return None
    if agency in AGENCY_SHORT_NAMES:
        return AGENCY_SHORT_NAMES[agency]
    # For unknown agencies, use as-is if short enough, else truncate at comma
    if len(agency) <= 30:
        return agency
    short = agency.split(",")[0].split(" - ")[0].strip()
    return short if short else agency[:30]


def select_template_for_lead(lead, templates):
    """Pick the best approved template for a lead based on segment.

    Priority (WIN-314):
    1. Conversation-first templates (random A/B/C rotation) — if any approved and lead has city
    2. remodelar_agentes_agency — if agency known and template approved (legacy fallback)
    3. remodelar_agentes_lisboa / _porto — if region matches and template approved (legacy fallback)
    4. remodelar_agentes_outreach — final fallback (always approved)

    Old pitch templates are deprecated: kept in file but not selected by default.

    Returns (template_name, variables_dict).
    """
    city = (lead.get("city") or "").strip()
    region = (lead.get("region") or "").strip()
    agency = (lead.get("agency") or "").strip()
    first_name = extract_first_name(lead.get("name", ""))

    # 1. Prefer approved conversation templates (random A/B/C rotation) — WIN-314
    if city:
        approved_conversa = [
            name for name, _variant in CONVERSA_TEMPLATES
            if name in templates and templates[name]["status"] == "approved" and templates[name].get("sid")
        ]
        if approved_conversa:
            chosen = random.choice(approved_conversa)
            return chosen, {"1": first_name, "2": city}

    # Try agency-specific template
    if agency and "remodelar_agentes_agency" in templates:
        t = templates["remodelar_agentes_agency"]
        if t["status"] == "approved" and t.get("sid"):
            short_agency = get_agency_short_name(agency)
            if short_agency and city:
                return t["name"], {"1": first_name, "2": short_agency, "3": city}

    # Try region-specific templates
    if city in LISBOA_REGION_CITIES or region == "Lisboa":
        if "remodelar_agentes_lisboa" in templates:
            t = templates["remodelar_agentes_lisboa"]
            if t["status"] == "approved" and t.get("sid"):
                display_city = city if city else "Lisboa"
                return t["name"], {"1": first_name, "2": display_city}

    if city in PORTO_REGION_CITIES or region == "Porto":
        if "remodelar_agentes_porto" in templates:
            t = templates["remodelar_agentes_porto"]
            if t["status"] == "approved" and t.get("sid"):
                display_city = city if city else "Porto"
                return t["name"], {"1": first_name, "2": display_city}

    # Fallback to generic approved template
    return "remodelar_agentes_outreach", {"1": first_name}


# --- Follow-up sequence logic (WIN-251) ---

# Follow-up touch definitions: (template_name, min_days_after_initial, max_days_after_initial)
FOLLOWUP_SEQUENCE = [
    {"touch": 1, "template": "remodelar_agentes_followup", "min_days": 2, "max_days": 4},
    {"touch": 2, "template": "remodelar_agentes_closing", "min_days": 5, "max_days": 8},
]

# Stages that should NOT receive follow-ups
FOLLOWUP_EXCLUDE_STAGES = {"opted_out", "demo_requested", "interested", "client"}


def find_followup_eligible(sent_log, templates, touch_num, db_path=None):
    """Find leads eligible for a specific follow-up touch.

    Returns list of (phone, name, agency, initial_sent_at) for leads that:
    - Received initial outreach N days ago (within touch window)
    - Have NOT received this follow-up touch template yet
    - Are NOT in an excluded contact stage
    """
    touch_def = next((t for t in FOLLOWUP_SEQUENCE if t["touch"] == touch_num), None)
    if not touch_def:
        return []

    template_name = touch_def["template"]
    if template_name not in templates or templates[template_name]["status"] != "approved":
        return []  # Template not approved yet

    now = datetime.now(timezone.utc)
    min_delta = timedelta(days=touch_def["min_days"])
    max_delta = timedelta(days=touch_def["max_days"])

    # Get excluded phones from contact stages
    excluded_phones = set()
    if db_path:
        import sqlite3
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.execute(
                "SELECT phone FROM contact_stages WHERE stage IN ({})".format(
                    ",".join("?" for _ in FOLLOWUP_EXCLUDE_STAGES)
                ),
                list(FOLLOWUP_EXCLUDE_STAGES)
            )
            excluded_phones = {row[0] for row in cursor.fetchall()}
            conn.close()
        except Exception:
            pass

    # Outreach template names
    outreach_templates = {
        "remodelar_agentes_outreach", "remodelar_agentes_lisboa",
        "remodelar_agentes_porto", "remodelar_agentes_agency",
    }

    eligible = []
    for phone, entries in sent_log.items():
        if phone in excluded_phones:
            continue

        # Find initial outreach send time
        initial_send = None
        for e in entries:
            if e.get("template_name") in outreach_templates and e.get("status") == "sent":
                try:
                    initial_send = datetime.fromisoformat(e["sent_at"].rstrip("Z")).replace(tzinfo=timezone.utc)
                except (ValueError, KeyError):
                    pass
                break

        if not initial_send:
            continue

        # Check time window
        elapsed = now - initial_send
        if elapsed < min_delta or elapsed > max_delta:
            continue

        # Check if this touch was already sent
        already_sent = any(
            e.get("template_name") == template_name and e.get("status") == "sent"
            for e in entries
        )
        if already_sent:
            continue

        # Get name/agency from any entry
        name = next((e.get("name", "") for e in entries if e.get("name")), "")
        agency = next((e.get("agency", "") for e in entries if e.get("agency")), "")

        eligible.append({
            "phone": phone,
            "name": name,
            "agency": agency,
            "initial_sent_at": initial_send.isoformat(),
        })

    return eligible


def get_ramp_cap(state):
    """Return daily cap from ramp schedule (WIN-314): 5/day wk1-2, 10/day wk3-4, 15/day wk5+."""
    ramp_start = state.get("ramp_start_date")
    if not ramp_start:
        return 5  # default: week 1-2 rate until ramp starts
    try:
        start = date.fromisoformat(ramp_start)
    except (ValueError, TypeError):
        return 5
    weeks_elapsed = (date.today() - start).days // 7
    if weeks_elapsed < 2:
        return 5
    elif weeks_elapsed < 4:
        return 10
    else:
        return 15


def load_sender_state():
    """Load one-shot sender state. Returns dict with defaults if missing."""
    defaults = {"next_send_at": None, "paused": False, "paused_at": None, "paused_reason": None, "ramp_start_date": None}
    if not SENDER_STATE_FILE.exists():
        return defaults
    try:
        with open(SENDER_STATE_FILE) as f:
            data = json.load(f)
        return {**defaults, **data}
    except (json.JSONDecodeError, OSError):
        return defaults


def save_sender_state(state):
    SENDER_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SENDER_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def schedule_next_send(state):
    """Set next_send_at to now + random 2-5 min interval."""
    delay = random.randint(ONE_SHOT_MIN_DELAY, ONE_SHOT_MAX_DELAY)
    next_time = datetime.now(timezone.utc) + timedelta(seconds=delay)
    state["next_send_at"] = next_time.isoformat()
    return delay


def is_time_to_send(state):
    """Return True if next_send_at is unset or in the past."""
    if not state.get("next_send_at"):
        return True
    try:
        next_send = datetime.fromisoformat(state["next_send_at"])
        return datetime.now(timezone.utc) >= next_send
    except ValueError:
        return True


def post_paperclip_alert(error_code, error_msg, phone, template_name):
    """Post a comment on WIN-75 using the CEO API key to alert the Chief."""
    api_key = os.environ.get("PAPERCLIP_CEO_API_KEY", "")
    if not api_key:
        print(f"  [ALERT] No PAPERCLIP_CEO_API_KEY — cannot post Paperclip alert.", file=sys.stderr)
        return
    comment = (
        f"## WhatsApp Outreach Paused — Rate Limit Error\n\n"
        f"Automated alert from `outreach_sender.py --one-shot`:\n\n"
        f"- **Error code:** `{error_code}`\n"
        f"- **Error message:** {error_msg}\n"
        f"- **Phone:** `{phone}`\n"
        f"- **Template:** `{template_name}`\n"
        f"- **Action taken:** All further sends paused (`sender_state.json` `paused=true`).\n\n"
        f"Chief: please investigate and manually reset `paused` to `false` in "
        f"`projects/whatsapp/data/sender_state.json` when it is safe to resume."
    )
    payload = json.dumps({"body": comment}).encode()
    url = f"{PAPERCLIP_API_URL}/api/issues/{WIN75_ISSUE_ID}/comments"
    req = request.Request(url, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    try:
        with request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
            print(f"  [ALERT] Paperclip alert posted (status {resp.status}).")
    except Exception as exc:
        print(f"  [ALERT] Failed to post Paperclip alert: {exc}", file=sys.stderr)


def run_follow_up(args, account_sid, api_key_sid, api_key_secret, from_number, templates):
    """Send follow-up messages to eligible leads (WIN-251)."""
    state = load_sender_state()

    if state.get("paused"):
        print(f"Outreach is PAUSED (reason: {state.get('paused_reason', 'unknown')}). Exiting.")
        sys.exit(0)

    if not is_business_hours() and not args.dry_run:
        now_lisbon = datetime.now(LISBON_TZ)
        print(f"Outside business hours ({now_lisbon.strftime('%H:%M %Z')}). Exiting.")
        sys.exit(0)

    # Compute effective daily cap: use ramp schedule if no explicit cap given (WIN-314)
    effective_daily_cap = args.daily_cap if args.daily_cap is not None else get_ramp_cap(state)

    touch_num = args.follow_up
    touch_def = next((t for t in FOLLOWUP_SEQUENCE if t["touch"] == touch_num), None)
    if not touch_def:
        print(f"ERROR: Unknown touch number {touch_num}", file=sys.stderr)
        sys.exit(1)

    template_name = touch_def["template"]
    if template_name not in templates:
        print(f"ERROR: Template '{template_name}' not found in templates.json", file=sys.stderr)
        sys.exit(1)

    fu_template = templates[template_name]
    if fu_template["status"] != "approved":
        print(f"Template '{template_name}' is '{fu_template['status']}' — not yet approved by Meta. Cannot send.")
        sys.exit(0)

    sent_log = load_sent_log()
    sent_today = count_sent_today(sent_log)

    db_path = SCRIPT_DIR / "data" / "whatsapp.db"
    eligible = find_followup_eligible(sent_log, templates, touch_num,
                                       db_path=str(db_path) if db_path.exists() else None)

    print(f"Follow-up touch {touch_num} ({template_name})")
    print(f"  Window: {touch_def['min_days']}-{touch_def['max_days']} days after initial outreach")
    print(f"  Eligible leads: {len(eligible)}")
    print(f"  Sent today: {sent_today}/{effective_daily_cap}")

    if not eligible:
        print("No eligible leads for this follow-up touch. Exiting.")
        sys.exit(0)

    sent_count = 0
    for lead_info in eligible:
        if sent_today + sent_count >= effective_daily_cap:
            print(f"  STOP: Daily cap of {effective_daily_cap} reached")
            break

        phone = lead_info["phone"]
        name = lead_info["name"]
        agency = lead_info["agency"]
        first_name = extract_first_name(name)
        variables = {"1": first_name}

        if args.dry_run:
            print(f"  [DRY RUN] Would send touch {touch_num} '{template_name}' to {name} ({phone})")
            sent_count += 1
            continue

        if args.one_shot and sent_count >= 1:
            break  # one-shot: only 1 message

        print(f"  Sending touch {touch_num} to {name} ({phone})...", end=" ", flush=True)
        resp = send_template(account_sid, api_key_sid, api_key_secret, from_number, phone,
                             fu_template["sid"], variables)

        twilio_sid = resp.get("sid", "")
        status = resp.get("status", "")
        error_msg = resp.get("message") or resp.get("error_message") or ""

        entry = {
            "phone": phone,
            "name": name,
            "agency": agency,
            "template_name": template_name,
            "template_sid": fu_template["sid"],
            "variant": TEMPLATE_VARIANT_MAP.get(template_name, ""),
            "sent_at": datetime.utcnow().isoformat() + "Z",
            "twilio_sid": twilio_sid,
            "status": "sent" if status in ("queued", "sent") else "failed",
            "error": error_msg if status not in ("queued", "sent") else "",
        }
        append_sent_log(entry)
        _db = whatsapp_db.get_db()
        whatsapp_db.add_outreach_message(_db, entry["phone"], entry["template_name"],
                                         entry["template_sid"], entry["twilio_sid"],
                                         entry["status"], entry["error"], entry["sent_at"])
        _db.commit()
        _db.close()

        if status in ("queued", "sent"):
            print(f"OK (SID: {twilio_sid})")
            sent_count += 1
        else:
            print(f"FAILED (error={error_msg})")

        if not args.one_shot:
            time.sleep(RATE_LIMIT_DELAY)

    if args.one_shot and sent_count > 0:
        delay = schedule_next_send(state)
        save_sender_state(state)
        print(f"Next send in {delay}s.")

    print(f"\nFollow-up touch {touch_num}: {sent_count} sent out of {len(eligible)} eligible")


def run_one_shot(args, account_sid, api_key_sid, api_key_secret, from_number, templates):
    """Send at most 1 message, respecting 2-5 min random intervals via state file."""
    state = load_sender_state()

    if state.get("paused"):
        print(f"Outreach is PAUSED (reason: {state.get('paused_reason', 'unknown')}). Exiting.")
        sys.exit(0)

    if not is_business_hours():
        now_lisbon = datetime.now(LISBON_TZ)
        print(f"Outside business hours ({now_lisbon.strftime('%H:%M %Z')}). Exiting.")
        sys.exit(0)

    if not is_time_to_send(state):
        next_send = state["next_send_at"]
        print(f"Not time yet. Next send scheduled at {next_send}. Exiting.")
        sys.exit(0)

    # Compute effective daily cap: use ramp schedule if no explicit cap given (WIN-314)
    effective_daily_cap = args.daily_cap if args.daily_cap is not None else get_ramp_cap(state)

    template = templates[args.template]
    leads = load_leads(args.leads_file)
    sent_log = load_sent_log()
    sent_today = count_sent_today(sent_log)

    if sent_today >= effective_daily_cap:
        print(f"Daily cap reached ({sent_today}/{effective_daily_cap}). Exiting.")
        sys.exit(0)

    NON_PROSPECTABLE_STATUSES = {"cliente", "client", "customer", "do_not_contact"}

    # Outreach template names for dedup (any initial outreach counts)
    OUTREACH_TEMPLATES = {
        "remodelar_agentes_outreach", "remodelar_agentes_lisboa",
        "remodelar_agentes_porto", "remodelar_agentes_agency",
    }

    # Find next unsent lead
    target_lead = None
    for lead in leads:
        phone = lead.get("phone", "").strip()
        if not phone:
            continue
        if lead.get("status", "").strip().lower() in NON_PROSPECTABLE_STATUSES:
            continue
        if not phone.startswith("+"):
            phone = "+" + phone.lstrip("0")
        if phone in sent_log:
            if args.personalized:
                # In personalized mode, skip if any outreach template was sent
                already_sent = any(
                    e.get("template_name") in OUTREACH_TEMPLATES and e.get("status") == "sent"
                    for e in sent_log[phone]
                )
            else:
                already_sent = any(
                    e.get("template_name") == args.template and e.get("status") == "sent"
                    for e in sent_log[phone]
                )
            if already_sent:
                continue
        target_lead = {**lead, "phone": phone}
        break

    if target_lead is None:
        print("No unsent leads remaining. Exiting.")
        sys.exit(0)

    phone = target_lead["phone"]
    name = target_lead.get("name", "").strip()
    agency = target_lead.get("agency", "").strip()

    # Personalized template selection (WIN-250)
    if args.personalized:
        selected_name, variables = select_template_for_lead(target_lead, templates)
        template = templates[selected_name]
    else:
        first_name = extract_first_name(name)
        variables = {"1": first_name}

    if args.dry_run:
        print(f"[DRY RUN] Would send '{template['name']}' to {name} ({phone}) vars={variables}")
        delay = schedule_next_send(state)
        save_sender_state(state)
        print(f"Next send scheduled in {delay}s.")
        return

    print(f"Sending '{template['name']}' to {name} ({phone})...", end=" ", flush=True)
    resp = send_template(account_sid, api_key_sid, api_key_secret, from_number, phone,
                         template["sid"], variables)

    twilio_sid = resp.get("sid", "")
    status = resp.get("status", "")
    error_msg = resp.get("message") or resp.get("error_message") or ""
    error_code = resp.get("code") or resp.get("error_code") or 0
    try:
        error_code = int(error_code)
    except (ValueError, TypeError):
        error_code = 0

    actual_template_name = template["name"]
    entry = {
        "phone": phone,
        "name": name,
        "agency": agency,
        "template_name": actual_template_name,
        "template_sid": template["sid"],
        "variant": TEMPLATE_VARIANT_MAP.get(actual_template_name, ""),
        "sent_at": datetime.utcnow().isoformat() + "Z",
        "twilio_sid": twilio_sid,
        "status": "sent" if status in ("queued", "sent") else "failed",
        "error": error_msg if status not in ("queued", "sent") else "",
    }
    append_sent_log(entry)
    # Dual-write to SQLite
    _db = whatsapp_db.get_db()
    whatsapp_db.add_outreach_message(_db, entry["phone"], entry["template_name"],
                                     entry["template_sid"], entry["twilio_sid"],
                                     entry["status"], entry["error"], entry["sent_at"])
    whatsapp_db.update_contact_stage(_db, entry["phone"], "contacted", "system")
    _db.commit()
    _db.close()

    if status in ("queued", "sent"):
        print(f"OK (SID: {twilio_sid})")
        # Set ramp start date on first successful send (WIN-314)
        if not state.get("ramp_start_date"):
            state["ramp_start_date"] = date.today().isoformat()
        delay = schedule_next_send(state)
        save_sender_state(state)
        print(f"Sent today: {sent_today + 1}/{effective_daily_cap}. Next send in {delay}s.")
    else:
        print(f"FAILED (status={status}, code={error_code}, error={error_msg})")
        if error_code in RATE_LIMIT_ERROR_CODES:
            state["paused"] = True
            state["paused_at"] = datetime.utcnow().isoformat() + "Z"
            state["paused_reason"] = f"Error {error_code}: {error_msg}"
            save_sender_state(state)
            print(f"RATE LIMIT ERROR {error_code} — outreach PAUSED. Posting Paperclip alert.")
            post_paperclip_alert(error_code, error_msg, phone, actual_template_name)
        else:
            # Non-rate-limit failure: still schedule next send
            delay = schedule_next_send(state)
            save_sender_state(state)
            print(f"Non-critical failure. Next attempt in {delay}s.")


def main():
    parser = argparse.ArgumentParser(description="WhatsApp Outreach Sender")
    parser.add_argument("--template", default="remodelar_agentes_outreach",
                        help="Template name to use (default: remodelar_agentes_outreach)")
    parser.add_argument("--daily-cap", type=int, default=None,
                        help="Max messages to send per day (default: ramp schedule — 5/day wk1-2, 10 wk3-4, 15 wk5+)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be sent without actually sending")
    parser.add_argument("--leads-file", type=Path, default=LEADS_FILE,
                        help=f"Path to leads CSV (default: {LEADS_FILE})")
    parser.add_argument("--one-shot", action="store_true",
                        help="Send at most 1 message per run; tracks timing in sender_state.json")
    parser.add_argument("--personalized", action="store_true",
                        help="Select best approved template per lead based on segment (WIN-250)")
    parser.add_argument("--follow-up", type=int, choices=[1, 2], default=None, metavar="TOUCH",
                        help="Send follow-up touch N (1=48-72h, 2=5-7d) to eligible leads (WIN-251)")
    args = parser.parse_args()

    load_env()

    if not args.dry_run:
        account_sid = require_env("TWILIO_ACCOUNT_SID")
        api_key_sid = require_env("TWILIO_API_KEY_SID")
        api_key_secret = require_env("TWILIO_API_KEY_SECRET")
        from_number = require_env("TWILIO_WHATSAPP_FROM")
    else:
        account_sid = api_key_sid = api_key_secret = from_number = ""

    templates = load_templates()

    # Compute effective daily cap (ramp schedule if no explicit --daily-cap given) — WIN-314
    state = load_sender_state()
    if args.daily_cap is None:
        args.daily_cap = get_ramp_cap(state)

    # Follow-up mode (WIN-251)
    if args.follow_up is not None:
        run_follow_up(args, account_sid, api_key_sid, api_key_secret, from_number, templates)
        return

    if args.template not in templates:
        print(f"ERROR: Template '{args.template}' not found in {TEMPLATES_FILE}", file=sys.stderr)
        print(f"Available templates: {', '.join(templates.keys())}", file=sys.stderr)
        sys.exit(1)

    if args.one_shot:
        run_one_shot(args, account_sid, api_key_sid, api_key_secret, from_number, templates)
        return

    if not args.personalized:
        template = templates[args.template]
        if template["status"] not in ("approved",):
            print(f"WARNING: Template '{args.template}' status is '{template['status']}' (not 'approved')")
            print("Meta may reject messages sent with unapproved templates.")
    else:
        template = None  # selected per-lead below

    leads = load_leads(args.leads_file)
    sent_log = load_sent_log()
    sent_today = count_sent_today(sent_log)

    print(f"Loaded {len(leads)} leads from {args.leads_file}")
    if template:
        print(f"Template: {template['name']} (SID: {template['sid']}, status: {template['status']})")
    else:
        print(f"Mode: personalized (best template per lead segment)")
    print(f"Sent today: {sent_today} / {args.daily_cap} daily cap")

    sent_count = 0
    skipped_count = 0
    failed_count = 0

    now_lisbon = datetime.now(LISBON_TZ)
    in_hours = is_business_hours()
    print(f"Business-hours check: {now_lisbon.strftime('%Y-%m-%d %H:%M %Z')} — {'OPEN (09:00-18:00)' if in_hours else 'CLOSED (outside 09:00-18:00)'}")
    if not in_hours and not args.dry_run:
        next_open = now_lisbon.replace(hour=BUSINESS_HOURS_START, minute=0, second=0, microsecond=0)
        if now_lisbon.hour >= BUSINESS_HOURS_END:
            next_open = next_open + timedelta(days=1)
        print(f"Outside business hours. Next window opens at {next_open.strftime('%Y-%m-%d %H:%M %Z')}. Exiting.")
        sys.exit(0)

    NON_PROSPECTABLE_STATUSES = {"cliente", "client", "customer", "do_not_contact"}
    OUTREACH_TEMPLATES_BATCH = {
        "remodelar_agentes_outreach", "remodelar_agentes_lisboa",
        "remodelar_agentes_porto", "remodelar_agentes_agency",
    }

    for lead in leads:
        phone = lead.get("phone", "").strip()
        name = lead.get("name", "").strip()
        agency = lead.get("agency", "").strip()

        if not phone:
            print(f"  SKIP (no phone): {name}")
            skipped_count += 1
            continue

        if lead.get("status", "").strip().lower() in NON_PROSPECTABLE_STATUSES:
            print(f"  SKIP (client status): {name} {phone}")
            skipped_count += 1
            continue

        # Normalize phone (ensure + prefix)
        if not phone.startswith("+"):
            phone = "+" + phone.lstrip("0")

        # Skip if already sent outreach to this phone
        if phone in sent_log:
            if args.personalized:
                already_sent = any(
                    e.get("template_name") in OUTREACH_TEMPLATES_BATCH and e.get("status") == "sent"
                    for e in sent_log[phone]
                )
            else:
                already_sent = any(
                    e.get("template_name") == args.template and e.get("status") == "sent"
                    for e in sent_log[phone]
                )
            if already_sent:
                print(f"  SKIP (already sent): {name} {phone}")
                skipped_count += 1
                continue

        # Enforce daily cap
        if sent_today + sent_count >= args.daily_cap:
            print(f"  STOP: Daily cap of {args.daily_cap} reached")
            break

        # Select template and variables
        if args.personalized:
            selected_name, variables = select_template_for_lead(lead, templates)
            lead_template = templates[selected_name]
        else:
            lead_template = template
            first_name = extract_first_name(name)
            variables = {"1": first_name}

        if args.dry_run:
            print(f"  [DRY RUN] Would send '{lead_template['name']}' to {name} ({phone}) vars={variables}")
            sent_count += 1
            continue

        print(f"  Sending '{lead_template['name']}' to {name} ({phone})...", end=" ", flush=True)
        resp = send_template(account_sid, api_key_sid, api_key_secret, from_number, phone,
                             lead_template["sid"], variables)

        twilio_sid = resp.get("sid", "")
        status = resp.get("status", "")
        error = resp.get("message") or resp.get("error_message") or resp.get("code", "")

        entry = {
            "phone": phone,
            "name": name,
            "agency": agency,
            "template_name": lead_template["name"],
            "template_sid": lead_template["sid"],
            "variant": TEMPLATE_VARIANT_MAP.get(lead_template["name"], ""),
            "sent_at": datetime.utcnow().isoformat() + "Z",
            "twilio_sid": twilio_sid,
            "status": "sent" if status in ("queued", "sent") else "failed",
            "error": error if status not in ("queued", "sent") else "",
        }
        append_sent_log(entry)
        # Dual-write to SQLite
        _db = whatsapp_db.get_db()
        whatsapp_db.add_outreach_message(_db, entry["phone"], entry["template_name"],
                                         entry["template_sid"], entry["twilio_sid"],
                                         entry["status"], entry["error"], entry["sent_at"])
        whatsapp_db.update_contact_stage(_db, entry["phone"], "contacted", "system")
        _db.commit()
        _db.close()

        if status in ("queued", "sent"):
            print(f"OK (SID: {twilio_sid})")
            sent_count += 1
        else:
            print(f"FAILED (status={status}, error={error})")
            failed_count += 1

        time.sleep(RATE_LIMIT_DELAY)

    print()
    print(f"Done: {sent_count} sent, {skipped_count} skipped, {failed_count} failed")
    print(f"Sent log: {SENT_LOG_FILE}")


if __name__ == "__main__":
    main()
