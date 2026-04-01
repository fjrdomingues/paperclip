#!/usr/bin/env python3
"""
WhatsApp Conversation Flow Handler (WIN-315)
LLM-powered reply handler for conversation-first outreach.

Processes inbound replies from real estate agents who received a
conversation-first opener (variants A/B/C from WIN-313). Classifies
the conversation stage and generates contextual responses using Claude AI.

State machine stages:
  opener_sent → engaged → qualified → pitched → converted / declined / silent
  Any stage → blacklisted (hostile/spam)

Rules:
  - Never mention Remodelar before 'qualified' stage
  - Max 3-4 lines per reply, PT-PT, one topic, ≤1 emoji
  - 48h silence after last outbound → mark silent, no further contact
  - Polite decline → graceful goodbye, mark declined
  - Hostile/spam → blacklist, no response

Usage:
  python conversation_handler.py [--dry-run] [--phone +351XXXXXXXXX]
  python conversation_handler.py [--dry-run] [--check-silence]

Env vars (loaded from projects/telegram/.env):
  TWILIO_ACCOUNT_SID, TWILIO_API_KEY_SID, TWILIO_API_KEY_SECRET, TWILIO_WHATSAPP_FROM
  ANTHROPIC_API_KEY
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import ssl
import base64
from urllib import request, parse
from urllib.error import HTTPError

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

import anthropic
import db as whatsapp_db

SCRIPT_DIR = Path(__file__).parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
ENV_FILE = PROJECT_ROOT / "projects" / "telegram" / ".env"
DATA_DIR = SCRIPT_DIR / "data"
INBOX_FILE = DATA_DIR / "inbox.jsonl"
TEMPLATES_FILE = SCRIPT_DIR / "templates.json"

# Stages that are terminal — no further automated responses
TERMINAL_STAGES = {"converted", "declined", "silent", "blacklisted", "opted_out"}

# Stage ordering for A/B analysis logging
STAGE_ORDER = ["opener_sent", "engaged", "qualified", "pitched", "converted", "declined", "silent", "blacklisted"]
WARM_LEAD_STAGES = {"interested", "demo_requested"}

RATE_LIMIT_DELAY = 2.0  # seconds between Twilio sends
SILENCE_HOURS = 48  # hours of no inbound before marking silent

# Variant → opener body map (for LLM context)
OPENER_BODIES = {
    "A": (
        "Olá {name}, vi que tem imóveis em {city}. Tenho acompanhado a zona e reparei que há muita diferença "
        "no tempo de venda dependendo da apresentação das fotos. Na sua experiência, os compradores reagem "
        "mais quando o imóvel está mobilado ou vazio?"
    ),
    "B": (
        "Olá {name}, encontrei os seus anúncios no Idealista enquanto fazia uma pesquisa sobre o mercado em {city}. "
        "Uma curiosidade — como costuma preparar os imóveis antes de fotografar para os portais? "
        "Faz algum tipo de staging ou apresenta tal como está?"
    ),
    "C": (
        "Olá {name}, tenho conversado com agentes imobiliários em {city} sobre um tema que aparece sempre: "
        "imóveis que ficam semanas nos portais sem gerar visitas. É algo que encontra no seu dia-a-dia, "
        "ou na sua zona o mercado está a mexer bem?"
    ),
}

LEGACY_OUTREACH_CONTEXTS = {
    "remodelar_agentes_outreach": (
        "A mensagem inicial ja apresentou a Remodelar AI, explicou que fazemos home staging virtual "
        "com IA e ofereceu 10 fotos gratuitas se o agente enviar uma foto de um imovel."
    ),
}

AUTO_RESPONDER_PATTERNS = (
    "o seu contato é muito importante para nós",
    "o seu contacto é muito importante para nós",
    "de momento, estamos ausentes",
    "de momento estou ausente",
    "não estou disponível de momento",
    "nao estou disponivel de momento",
    "não estou disponível",
    "nao estou disponivel",
    "responderei à sua mensagem o mais breve possível",
    "responderei a sua mensagem o mais breve possível",
    "responderei assim que possível",
    "responderei assim que possivel",
    "entrarei em contacto consigo assim que estiver disponível",
    "estarei à sua disposição",
    "respondemos com a maior brevidade possível",
)


def _load_template_bodies():
    if not TEMPLATES_FILE.exists():
        return {}
    try:
        with open(TEMPLATES_FILE) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return {
        item.get("name"): item.get("body", "")
        for item in data.get("templates", [])
        if item.get("name")
    }


TEMPLATE_BODY_BY_NAME = _load_template_bodies()

SYSTEM_PROMPT = """Você é um assistente a gerir conversas de WhatsApp para a Remodelar AI,
uma empresa de home staging virtual com IA que serve agentes imobiliários em Portugal.

O objetivo é construir uma conversa genuína com o agente antes de apresentar o produto.

REGRAS ABSOLUTAS:
1. Respeite o tipo de abordagem indicado no prompt do utilizador:
   - conversation_first: NUNCA mencione "Remodelar", staging virtual, IA ou preços antes do stage "qualified".
   - legacy_outreach: a primeira mensagem ja apresentou a Remodelar AI; responda diretamente ao pedido do agente sem repetir um pitch agressivo.
2. Máximo 3-4 linhas por mensagem. Seja conciso.
3. Escreva em Português Europeu (PT-PT). Use "você" para tratar o agente.
4. Máximo 1 emoji por mensagem. Prefira zero emojis.
5. Um único tópico ou pergunta por mensagem.
6. Tom: profissional mas amigável. Não excessivamente formal, não casual.
7. Nunca envie múltiplas perguntas numa só mensagem.

STAGES DA CONVERSA:
- opener_sent: Enviámos a mensagem de abertura. O agente ainda não respondeu.
- engaged: O agente respondeu. Estamos em conversa genuína (1-3 trocas).
- qualified: O agente mostrou interesse no tema de staging/apresentação de fotos.
- pitched: Apresentámos a Remodelar de forma suave.
- converted: O agente quer experimentar / pediu demo.
- declined: O agente recusou educadamente.
- silent: Sem resposta por 48h após a última mensagem enviada.
- blacklisted: Hostil, spam, pediu para não ser contactado.
- opted_out: Pediu explicitamente para parar.

TRANSIÇÕES DE STAGE:
- opener_sent → engaged: quando o agente responde genuinamente.
- engaged → qualified: quando o agente mostra curiosidade ou interesse em staging/apresentação de imóveis.
- engaged → declined: quando o agente recusa educadamente continuar a conversa.
- engaged → blacklisted: quando o agente é hostil ou pede para não ser contactado.
- qualified → pitched: após 2-3 trocas genuínas, apresentar a Remodelar com leveza.
- pitched → converted: o agente quer saber mais / experimentar.
- pitched → declined: o agente recusa após o pitch.
- any → opted_out: o agente pede explicitamente para parar.
- any → blacklisted: hostilidade / spam explícito.

COMO RESPONDER POR STAGE:

engaged (ainda sem mencionar Remodelar):
  - Aprofunde a conversa sobre o mercado imobiliário ou a questão levantada.
  - Faça uma pergunta de follow-up relevante.
  - Exemplos: perguntar sobre imóveis novos vs usados, sobre a zona, sobre desafios comuns.

qualified (o agente mostrou interesse em staging/apresentação):
  - Continue a aprofundar 1-2 trocas.
  - Depois, apresente a Remodelar de forma natural: "Nós trabalhamos exatamente nisto..."
  - Não seja vendedor. Ofereça uma demo gratuita sem pressão.

pitched (apresentámos a Remodelar):
  - Se o agente mostrar interesse: dê mais detalhes, ofereça enviar exemplos.
  - Se o agente não reagir: não pressione. Espere pela resposta deles.

declined:
  Resposta: "Compreendo perfeitamente. Se algum dia precisar, estou disponível. Boa sorte com as vendas!"
  Depois: não responder mais.

opted_out:
  Resposta: "Peço desculpa pelo incómodo. Não voltarei a contactar. Bom trabalho!"
  Depois: não responder mais.

blacklisted:
  NÃO responder. Marcar para blacklist.

FORMATO DE SAÍDA (JSON obrigatório):
{
  "new_stage": "<stage>",
  "should_respond": true/false,
  "response": "<mensagem ou null>",
  "reasoning": "<breve explicação da decisão>"
}
"""


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


def now_utc():
    return datetime.now(timezone.utc)


def parse_iso(ts_str):
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        return None


def load_inbox():
    """Load inbox.jsonl. Returns dict: phone -> list of message dicts, deduped by SID."""
    messages = {}
    seen_sids = set()
    if not INBOX_FILE.exists():
        return messages
    with open(INBOX_FILE) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                sid = msg.get("sid", "")
                if sid and sid in seen_sids:
                    continue
                if sid:
                    seen_sids.add(sid)
                phone = msg.get("from", "").strip()
                if not phone.startswith("+"):
                    phone = "+" + phone.lstrip("+")
                if phone not in messages:
                    messages[phone] = []
                messages[phone].append(msg)
            except json.JSONDecodeError:
                continue
    return messages


def send_free_text(account_sid, api_key_sid, api_key_secret, from_number, to_number, body):
    """Send a free-text WhatsApp message (within 24h conversation window)."""
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    to = f"whatsapp:{to_number}" if not to_number.startswith("whatsapp:") else to_number
    data = parse.urlencode({
        "From": from_number,
        "To": to,
        "Body": body,
    }).encode()
    req = request.Request(url, data=data, method="POST")
    credentials = base64.b64encode(f"{api_key_sid}:{api_key_secret}".encode()).decode()
    req.add_header("Authorization", f"Basic {credentials}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with request.urlopen(req, context=_SSL_CTX) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        raw = e.read().decode()
        return json.loads(raw) if raw else {"error_message": str(e), "status": "failed"}


def build_conversation_prompt(
    lead_name,
    lead_city,
    variant,
    history,
    new_inbound_body,
    *,
    approach_mode="conversation_first",
    campaign_context=None,
):
    """Build the user-turn prompt for Claude."""
    opener = OPENER_BODIES.get(variant, "").format(name=lead_name or "agente", city=lead_city or "Portugal")

    lines = [
        f"Conversa com agente: {lead_name or 'desconhecido'} ({lead_city or 'Portugal'})",
        f"Tipo de abordagem: {approach_mode}",
        f"Variante do opener: {variant or 'desconhecida'}",
        "",
        "=== HISTÓRICO DA CONVERSA ===",
    ]

    if campaign_context:
        lines.append(f"[CONTEXTO DE CAMPANHA] {campaign_context}")

    if opener and not history:
        lines.append(f"[OUTBOUND - opener] {opener}")

    for ex in history:
        direction = "OUTBOUND" if ex["direction"] == "outbound" else "INBOUND"
        body = ex.get("body") or ""
        if direction == "OUTBOUND":
            body = TEMPLATE_BODY_BY_NAME.get(body, body)
        lines.append(f"[{direction}] {body}")

    lines.append(f"[INBOUND - nova mensagem] {new_inbound_body}")
    lines.append("")
    lines.append("Analise a conversa e devolva o JSON de decisão.")

    return "\n".join(lines)


def classify_and_respond(
    client,
    lead_name,
    lead_city,
    variant,
    history,
    new_inbound_body,
    dry_run=False,
    *,
    approach_mode="conversation_first",
    campaign_context=None,
):
    """Call Claude to classify stage and generate response. Returns (new_stage, should_respond, response_text)."""
    user_prompt = build_conversation_prompt(
        lead_name,
        lead_city,
        variant,
        history,
        new_inbound_body,
        approach_mode=approach_mode,
        campaign_context=campaign_context,
    )

    if dry_run:
        print(f"    [DRY RUN] Would call Claude with prompt ({len(user_prompt)} chars)")
        return "engaged", True, "[DRY RUN response would appear here]"

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = message.content[0].text.strip()

        # Extract JSON from response (may be wrapped in markdown code fence)
        if "```" in raw:
            raw = raw.split("```")[1].lstrip("json").strip()

        result = json.loads(raw)
        new_stage = result.get("new_stage", "engaged")
        should_respond = result.get("should_respond", False)
        response_text = result.get("response") or None
        reasoning = result.get("reasoning", "")

        print(f"    Stage: {new_stage} | Respond: {should_respond} | Reason: {reasoning[:80]}")
        return new_stage, should_respond, response_text

    except (json.JSONDecodeError, KeyError, IndexError) as e:
        print(f"    WARNING: Could not parse Claude response: {e}")
        return "engaged", False, None
    except Exception as e:
        print(f"    ERROR calling Claude: {e}")
        return "engaged", False, None


def _get_processing_context(db, phone, current_stage):
    """Return the approach metadata for an eligible phone, or None when it should be skipped."""
    variant = whatsapp_db.get_opener_variant(db, phone)
    latest_outreach = whatsapp_db.get_latest_outreach_message(db, phone)
    latest_template = latest_outreach["template_name"] if latest_outreach else None
    exchange_count = whatsapp_db.get_exchange_count(db, phone)

    if variant:
        return {
            "approach_mode": "conversation_first",
            "variant": variant,
            "campaign_context": None,
        }

    if latest_template in LEGACY_OUTREACH_CONTEXTS and (
        current_stage in WARM_LEAD_STAGES or exchange_count > 0
    ):
        return {
            "approach_mode": "legacy_outreach",
            "variant": "legacy_outreach",
            "campaign_context": LEGACY_OUTREACH_CONTEXTS[latest_template],
        }

    return None


def _get_history_for_prompt(db, phone, pending_sids):
    """Prefer conversation-specific history; fall back to raw pipeline transcript when needed."""
    history = whatsapp_db.get_conversation_history(db, phone)
    if history:
        return history

    pending_sid_set = {sid for sid in pending_sids if sid}
    fallback_history = []
    for msg in whatsapp_db.get_messages_for_phone(db, phone):
        twilio_sid = msg.get("twilio_sid")
        if msg.get("direction") == "inbound" and twilio_sid and twilio_sid in pending_sid_set:
            continue
        fallback_history.append(msg)
    return fallback_history


def _is_probable_auto_responder(body):
    """Heuristic guard for obvious away messages / auto-replies."""
    if not body:
        return False
    normalized = " ".join(body.lower().split())
    matches = sum(1 for pattern in AUTO_RESPONDER_PATTERNS if pattern in normalized)
    return matches >= 2


def check_silence(db, dry_run=False):
    """Mark leads as silent if last outbound was >48h ago with no inbound reply."""
    cutoff = now_utc() - timedelta(hours=SILENCE_HOURS)
    # Find conversation leads whose stage is not terminal
    rows = db.execute(
        """SELECT cs.phone, cs.stage, l.name
           FROM contact_stages cs
           LEFT JOIN leads l ON cs.phone = l.phone
           WHERE cs.stage IN ('opener_sent', 'engaged', 'qualified', 'pitched')""",
    ).fetchall()

    silenced = 0
    for row in rows:
        phone = row["phone"]
        stage = row["stage"]
        name = row["name"] or phone

        # Check last outbound exchange
        last_out = db.execute(
            """SELECT sent_at FROM conversation_exchanges
               WHERE phone = ? AND direction = 'outbound'
               ORDER BY sent_at DESC LIMIT 1""",
            (phone,),
        ).fetchone()

        if not last_out:
            # Also check outreach_messages for the opener
            last_out = db.execute(
                """SELECT sent_at FROM outreach_messages
                   WHERE phone = ? AND template_name IN ('remodelar_conversa_mercado','remodelar_conversa_elogio','remodelar_conversa_desafio')
                     AND status = 'sent'
                   ORDER BY sent_at DESC LIMIT 1""",
                (phone,),
            ).fetchone()

        if not last_out:
            continue

        last_out_ts = parse_iso(last_out["sent_at"])
        if not last_out_ts:
            continue

        if last_out_ts > cutoff:
            # Still within window
            continue

        # Check if any inbound after last outbound
        has_reply = db.execute(
            """SELECT 1 FROM conversation_exchanges
               WHERE phone = ? AND direction = 'inbound' AND sent_at > ?
               LIMIT 1""",
            (phone, last_out["sent_at"]),
        ).fetchone()

        if has_reply:
            continue

        print(f"  SILENT: {name} ({phone}) — no reply for >{SILENCE_HOURS}h, marking silent")
        if not dry_run:
            whatsapp_db.update_contact_stage(db, phone, "silent", classified_by="silence_check")
        silenced += 1

    return silenced


def process_phone(db, phone, inbound_messages, client, twilio_creds, dry_run=False):
    """Process all new inbound messages for a single phone number."""
    lead = whatsapp_db.get_lead_by_phone(db, phone)
    lead_name = lead["name"] if lead else "agente"
    lead_city = lead["city"] if lead else "Portugal"

    stage_row = whatsapp_db.get_contact_stage(db, phone)
    current_stage = stage_row["stage"] if stage_row else "opener_sent"
    context = _get_processing_context(db, phone, current_stage)
    if not context:
        return 0

    if current_stage in TERMINAL_STAGES:
        return 0

    first_name = lead_name.split()[0] if lead_name and lead_name.strip() else "agente"

    pending_messages = []
    for msg in sorted(inbound_messages, key=lambda m: m.get("timestamp", "")):
        sid = msg.get("sid", "")
        body = msg.get("body", "").strip()

        if not body:
            continue

        if sid and whatsapp_db.has_processed_inbound(db, phone, sid):
            continue

        pending_messages.append(msg)

    if not pending_messages:
        return 0

    combined_body = "\n".join(msg.get("body", "").strip() for msg in pending_messages if msg.get("body", "").strip())
    pending_sids = [msg.get("sid", "") for msg in pending_messages]
    latest_body = pending_messages[-1].get("body", "").strip()
    latest_received_at = pending_messages[-1].get("timestamp", now_utc().isoformat())

    if _is_probable_auto_responder(latest_body):
        print(f"  Skipping probable auto-responder from {first_name} ({phone})")
        if not dry_run:
            for msg in pending_messages:
                whatsapp_db.add_conversation_exchange(
                    db,
                    phone,
                    "inbound",
                    msg.get("body", "").strip(),
                    msg.get("sid", ""),
                    current_stage,
                    context["variant"],
                    msg.get("timestamp", latest_received_at),
                )
            db.commit()
        return len(pending_messages)

    print(
        f"  Processing {len(pending_messages)} new message(s) from {first_name} ({phone}): "
        f"'{latest_body[:60]}'"
    )

    history = _get_history_for_prompt(db, phone, pending_sids)
    new_stage, should_respond, response_text = classify_and_respond(
        client,
        first_name,
        lead_city,
        context["variant"],
        history,
        combined_body,
        dry_run=dry_run,
        approach_mode=context["approach_mode"],
        campaign_context=context["campaign_context"],
    )

    if not dry_run:
        for msg in pending_messages:
            whatsapp_db.add_conversation_exchange(
                db,
                phone,
                "inbound",
                msg.get("body", "").strip(),
                msg.get("sid", ""),
                current_stage,
                context["variant"],
                msg.get("timestamp", latest_received_at),
            )
        db.commit()

    if new_stage != current_stage:
        print(f"    Stage transition: {current_stage} → {new_stage}")
        if not dry_run:
            whatsapp_db.update_contact_stage(db, phone, new_stage, classified_by="conversation_handler")
            db.commit()

    current_stage = new_stage

    if current_stage in ("blacklisted", "opted_out"):
        print(f"    → Adding to DNC (reason: {current_stage})")
        if not dry_run:
            _add_to_dnc(phone, lead_name, lead["agency"] if lead else "", current_stage, combined_body)
        return len(pending_messages)

    if should_respond and response_text:
        _send_response(
            db,
            phone,
            lead_name,
            context["variant"],
            current_stage,
            response_text,
            twilio_creds,
            dry_run=dry_run,
        )
        time.sleep(RATE_LIMIT_DELAY)

    return len(pending_messages)


def _send_response(db, phone, lead_name, variant, stage, response_text, twilio_creds, dry_run=False):
    if dry_run:
        print(f"    [DRY RUN] Would send: '{response_text[:80]}'")
        return

    account_sid, api_key_sid, api_key_secret, from_number = twilio_creds
    print(f"    Sending response...", end=" ", flush=True)
    resp = send_free_text(account_sid, api_key_sid, api_key_secret, from_number, phone, response_text)

    sid = resp.get("sid", "")
    status = resp.get("status", "")
    error = resp.get("message") or resp.get("error_message") or ""

    if status in ("queued", "sent"):
        print(f"OK (SID: {sid})")
        whatsapp_db.add_conversation_exchange(
            db, phone, "outbound", response_text, sid, stage, variant, now_utc().isoformat()
        )
        db.commit()
    else:
        print(f"FAILED (status={status}, error={error})")


def _add_to_dnc(phone, name, agency, reason, reply_body):
    """Append to DNC CSV."""
    import csv
    dnc_file = DATA_DIR / "dnc.csv"
    DNC_FIELDS = ["phone", "name", "agency", "reason", "reply_body", "added_at"]
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    file_exists = dnc_file.exists()
    with open(dnc_file, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=DNC_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "phone": phone, "name": name, "agency": agency,
            "reason": reason, "reply_body": reply_body[:200],
            "added_at": now_utc().isoformat(),
        })


def main():
    parser = argparse.ArgumentParser(description="WhatsApp Conversation Flow Handler")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen without sending or writing")
    parser.add_argument("--phone", help="Process only this phone number (+351...)")
    parser.add_argument("--check-silence", action="store_true",
                        help="Check for silent leads (48h no reply) and mark them")
    args = parser.parse_args()

    load_env()

    if not args.dry_run:
        account_sid = require_env("TWILIO_ACCOUNT_SID")
        api_key_sid = require_env("TWILIO_API_KEY_SID")
        api_key_secret = require_env("TWILIO_API_KEY_SECRET")
        from_number = require_env("TWILIO_WHATSAPP_FROM")
        twilio_creds = (account_sid, api_key_sid, api_key_secret, from_number)
    else:
        twilio_creds = None

    if args.dry_run:
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "dry-run-placeholder")
    else:
        anthropic_key = require_env("ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=anthropic_key)

    db = whatsapp_db.get_db()

    stats = {"processed": 0, "responded": 0, "silenced": 0, "skipped": 0}

    if args.check_silence:
        print("=== Checking for silent leads ===")
        stats["silenced"] = check_silence(db, dry_run=args.dry_run)
        print(f"  Silenced: {stats['silenced']}")

    print("=== Processing inbound messages ===")
    inbox = load_inbox()

    if args.phone:
        phones_to_process = [args.phone] if args.phone in inbox else []
        if not phones_to_process:
            print(f"  No inbound messages found for {args.phone}")
    else:
        phones_to_process = list(inbox.keys())

    print(f"  Found {len(inbox)} phones with inbound messages, checking {len(phones_to_process)}")

    for phone in phones_to_process:
        messages = inbox[phone]

        n = process_phone(db, phone, messages, client, twilio_creds, dry_run=args.dry_run)
        if n == 0:
            stats["skipped"] += 1
            continue
        stats["processed"] += n

    db.close()

    print()
    print("=== Summary ===")
    print(f"  Messages processed:  {stats['processed']}")
    print(f"  Leads skipped:       {stats['skipped']} (not conversation-first leads)")
    print(f"  Silenced:            {stats['silenced']}")
    if args.dry_run:
        print("  (dry run — no messages sent, no DB writes)")


if __name__ == "__main__":
    main()
