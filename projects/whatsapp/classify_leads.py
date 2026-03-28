#!/usr/bin/env python3
"""
Lead classifier tooling for WhatsApp pipeline (WIN-108).

Two-phase design for use by Paperclip agent Routines:
  1. `extract` — outputs conversations that need classification as JSON (no LLM needed)
  2. `apply`  — writes a classification result to SQLite

The Paperclip agent reads extract output, classifies using its own LLM,
then calls apply for each result. No external API key required.

Usage:
  python classify_leads.py extract [--force] [--limit N]
  python classify_leads.py apply --phone PHONE --stage STAGE --confidence 0.95 --reason "..." [quality flags]
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import db as whatsapp_db

VALID_STAGES = {
    "cold", "contacted", "replied", "auto_responder",
    "interested", "demo_requested", "opted_out",
}

QUALITY_DIMENSIONS = (
    "warmth",
    "timing",
    "relevance",
    "trust",
    "conversion_readiness",
)

CLASSIFICATION_PROMPT = """Classify this WhatsApp conversation into exactly ONE stage and score bot quality:
- auto_responder: Automated/generic reply (out-of-office, "Obrigado pelo contacto", no human engagement)
- opted_out: Explicitly not interested or asked to stop
- interested: Genuine human interest (asks questions, wants info)
- demo_requested: Wants a meeting, call, or appointment
- replied: Human reply that doesn't fit other categories

Context: Portuguese real estate renovation outreach. Many agents have auto-reply messages.
Key distinction: auto-responders (templated corporate replies) vs genuine human replies.

Quality rubric (score each 1-5, integers only):
- warmth: How warm/personable is the bot's tone? (1=robotic, 5=natural friendly)
- timing: Are messages well-timed and paced? (1=too fast/pushy, 5=natural cadence)
- relevance: Are bot responses relevant to what the lead said? (1=off-topic, 5=perfectly targeted)
- trust: Does the conversation build trust? (1=feels spammy, 5=professional trustworthy)
- conversion_readiness: Is the lead ready for next step? (1=cold, 5=ready to buy)

When ANY score is 3 or below, proposed_change MUST contain a concrete suggestion for improving the bot instructions.
That suggestion should be actionable: prompt diff, system instruction tweak, or template rewrite.

Respond with ONLY a JSON object in this shape:
{"stage":"<stage>","confidence":0.0,"reason":"<brief>","quality":{"warmth":1,"timing":1,"relevance":1,"trust":1,"conversion_readiness":1},"proposed_change":null}"""


def normalize_classification_payload(payload, require_quality=True):
    """Validate and normalize an LLM classification payload."""
    if not isinstance(payload, dict):
        raise ValueError("Classification payload must be a JSON object")

    stage = payload.get("stage")
    if stage not in VALID_STAGES:
        raise ValueError(f"Invalid stage '{stage}'")

    try:
        confidence = float(payload.get("confidence", 0.8))
    except (TypeError, ValueError) as exc:
        raise ValueError("Confidence must be a number between 0.0 and 1.0") from exc
    if confidence < 0.0 or confidence > 1.0:
        raise ValueError("Confidence must be between 0.0 and 1.0")

    reason_value = payload.get("reason", "")
    reason = "" if reason_value is None else str(reason_value).strip()
    quality = payload.get("quality")
    scores = None
    if quality is None:
        if require_quality:
            raise ValueError("Missing quality score object")
    else:
        if not isinstance(quality, dict):
            raise ValueError("Missing quality score object")
        scores = {}
        for key in QUALITY_DIMENSIONS:
            value = quality.get(key)
            if isinstance(value, bool):
                raise ValueError(f"Quality score '{key}' must be an integer from 1 to 5")
            try:
                score = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Quality score '{key}' must be an integer from 1 to 5") from exc
            if score < 1 or score > 5:
                raise ValueError(f"Quality score '{key}' must be an integer from 1 to 5")
            scores[key] = score

    proposed_change = payload.get("proposed_change")
    if proposed_change is None:
        proposed_change = None
    else:
        proposed_change = str(proposed_change).strip() or None

    if scores and any(score <= 3 for score in scores.values()) and not proposed_change:
        raise ValueError("proposed_change is required when any quality score is 3 or below")

    return {
        "stage": stage,
        "confidence": confidence,
        "reason": reason,
        "quality": scores,
        "proposed_change": proposed_change,
    }


def get_conversation_text(conn, phone):
    """Build a readable conversation transcript."""
    messages = whatsapp_db.get_messages_for_phone(conn, phone)
    if not messages:
        return None

    lines = []
    for msg in messages:
        direction = msg["direction"]
        body = msg["body"] or ""
        ts = msg["timestamp"] or ""
        if direction == "outbound":
            lines.append(f"[US → {phone}] ({ts}) Template: {body}")
        else:
            lines.append(f"[{phone} → US] ({ts}) {body}")

    return "\n".join(lines)


def serialize_quality_score(score_row):
    """Convert a quality score row into JSON-safe output."""
    if not score_row:
        return None

    return {
        "warmth": score_row["warmth"],
        "timing": score_row["timing"],
        "relevance": score_row["relevance"],
        "trust": score_row["trust"],
        "conversion_readiness": score_row["conversion_readiness"],
        "proposed_change": score_row["proposed_change"],
        "scored_at": score_row["scored_at"],
    }


def get_existing_quality_scores(current_stage, latest_quality_score):
    """Expose the most recent quality context available for the contact."""
    if latest_quality_score:
        return serialize_quality_score(latest_quality_score)

    if not current_stage:
        return None

    fallback_scores = {
        "warmth": current_stage["warmth_score"],
        "timing": current_stage["timing_score"],
        "relevance": current_stage["relevance_score"],
        "trust": current_stage["trust_score"],
        "conversion_readiness": current_stage["conversion_readiness_score"],
    }
    if all(value is None for value in fallback_scores.values()):
        return None

    return {
        **fallback_scores,
        "proposed_change": current_stage["quality_improvement_suggestion"],
        "scored_at": current_stage["classified_at"],
    }


def cmd_extract(args):
    """Output conversations that need classification as JSON."""
    conn = whatsapp_db.get_db()

    query = """
        WITH recent_activity AS (
            SELECT
                im.phone,
                MAX(datetime(im.received_at)) AS last_inbound_at
            FROM inbound_messages im
            INNER JOIN outreach_messages om ON im.phone = om.phone AND om.status = 'sent'
            GROUP BY im.phone
        )
        SELECT ra.phone, ra.last_inbound_at
        FROM recent_activity ra
        LEFT JOIN contact_stages cs ON ra.phone = cs.phone
    """

    if not args.force:
        query += """
        WHERE cs.classified_at IS NULL
           OR ra.last_inbound_at > datetime(cs.classified_at)
        """

    query += "\nORDER BY ra.last_inbound_at DESC"

    rows = conn.execute(query).fetchall()
    phones = [r["phone"] for r in rows]
    last_inbound_by_phone = {r["phone"]: r["last_inbound_at"] for r in rows}

    if args.limit:
        phones = phones[: args.limit]

    results = []
    for phone in phones:
        conversation = get_conversation_text(conn, phone)
        if not conversation:
            continue

        lead = whatsapp_db.get_lead_by_phone(conn, phone)
        current = whatsapp_db.get_contact_stage(conn, phone)
        latest_quality_score = whatsapp_db.get_latest_quality_score(conn, phone)

        results.append(
            {
                "phone": phone,
                "name": lead["name"] if lead else None,
                "agency": lead["agency"] if lead else None,
                "current_stage": current["stage"] if current else None,
                "classified_by": current["classified_by"] if current else None,
                "existing_quality_scores": get_existing_quality_scores(
                    current,
                    latest_quality_score,
                ),
                "last_inbound_at": last_inbound_by_phone.get(phone),
                "conversation": conversation,
            }
        )

    conn.close()

    output = {
        "count": len(results),
        "classification_prompt": CLASSIFICATION_PROMPT,
        "contacts": results,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


def cmd_apply(args):
    """Write a single classification result to SQLite."""
    quality = {
        "warmth": args.warmth,
        "timing": args.timing,
        "relevance": args.relevance,
        "trust": args.trust,
        "conversion_readiness": args.conversion_readiness,
    }
    provided_quality_keys = [key for key, value in quality.items() if value is not None]
    if provided_quality_keys and len(provided_quality_keys) != len(quality):
        missing = [key for key, value in quality.items() if value is None]
        print(
            "ERROR: quality scoring requires all score flags together; missing "
            + ", ".join(missing),
            file=sys.stderr,
        )
        sys.exit(1)

    payload_input = {
        "stage": args.stage,
        "confidence": args.confidence,
        "reason": args.reason,
    }
    if provided_quality_keys:
        payload_input["quality"] = quality
        payload_input["proposed_change"] = args.proposed_change

    try:
        payload = normalize_classification_payload(
            payload_input,
            require_quality=bool(provided_quality_keys),
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    conn = whatsapp_db.get_db()
    whatsapp_db.update_contact_stage(
        conn,
        args.phone,
        payload["stage"],
        "llm",
        payload["confidence"],
        payload["reason"],
        quality_scores=payload["quality"],
        quality_improvement_suggestion=payload["proposed_change"],
    )
    if payload["quality"] is not None:
        whatsapp_db.add_quality_score(
            conn,
            args.phone,
            payload["quality"]["warmth"],
            payload["quality"]["timing"],
            payload["quality"]["relevance"],
            payload["quality"]["trust"],
            payload["quality"]["conversion_readiness"],
            proposed_change=payload["proposed_change"],
        )
    conn.commit()
    conn.close()

    print(
        json.dumps(
            {
                "ok": True,
                "phone": args.phone,
                "stage": payload["stage"],
                "quality": payload["quality"],
                "proposed_change": payload["proposed_change"],
            }
        )
    )


def main():
    parser = argparse.ArgumentParser(description="Lead Classifier Tooling")
    sub = parser.add_subparsers(dest="command", required=True)

    # extract subcommand
    p_extract = sub.add_parser("extract", help="Output conversations that need classification as JSON")
    p_extract.add_argument(
        "--force",
        action="store_true",
        help="Include contacts even if they were already classified after their latest inbound message",
    )
    p_extract.add_argument("--limit", type=int, default=0, help="Max contacts (0 = all)")

    # apply subcommand
    p_apply = sub.add_parser("apply", help="Write a classification to SQLite")
    p_apply.add_argument("--phone", required=True, help="Contact phone number")
    p_apply.add_argument("--stage", required=True, help=f"One of: {', '.join(sorted(VALID_STAGES))}")
    p_apply.add_argument("--confidence", type=float, required=True, help="0.0-1.0")
    p_apply.add_argument("--reason", required=True, help="Brief classification reason")
    p_apply.add_argument("--warmth", type=int, help="1-5 quality score")
    p_apply.add_argument("--timing", type=int, help="1-5 quality score")
    p_apply.add_argument("--relevance", type=int, help="1-5 quality score")
    p_apply.add_argument("--trust", type=int, help="1-5 quality score")
    p_apply.add_argument(
        "--conversion-readiness",
        type=int,
        help="1-5 quality score",
    )
    p_apply.add_argument(
        "--proposed-change",
        default=None,
        help="Concrete bot instruction or template change when any quality score is 3 or below",
    )

    args = parser.parse_args()

    if args.command == "extract":
        cmd_extract(args)
    elif args.command == "apply":
        cmd_apply(args)


if __name__ == "__main__":
    main()
