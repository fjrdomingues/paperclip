#!/usr/bin/env python3
"""
Automated lead classifier — runs extract, classifies via local Claude CLI, applies results.
Designed to run via LaunchAgent every 2 hours.

Uses the claude CLI harness already installed on this machine (no API key needed).
"""
import json
import os
import subprocess
import sys
import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE = os.path.join(SCRIPT_DIR, "data", "classify.log")
CLAUDE_BIN = os.path.expanduser("~/.local/bin/claude")

sys.path.insert(0, SCRIPT_DIR)
import classify_leads


def log(msg):
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"{ts} {msg}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def classify_contact(prompt, contact):
    """Classify a single contact using the local claude CLI."""
    phone = contact["phone"]
    conversation = contact["conversation"]
    contact_context = {key: value for key, value in contact.items() if key != "conversation"}

    user_prompt = (
        f"{prompt}\n\n---\nContact context:\n"
        f"{json.dumps(contact_context, ensure_ascii=False, indent=2)}\n\n"
        f"Conversation:\n{conversation}\n\n"
        "Respond with ONLY the JSON object, no markdown fencing."
    )

    result = subprocess.run(
        [CLAUDE_BIN, "-p", user_prompt, "--model", "haiku",
         "--allowedTools", ""],
        capture_output=True, text=True, timeout=60
    )

    if result.returncode != 0:
        return None, f"claude CLI error: {result.stderr.strip()}"

    text = result.stdout.strip()
    # Strip markdown fencing if present
    if text.startswith("```"):
        text = "\n".join(text.split("\n")[1:])
    if text.endswith("```"):
        text = "\n".join(text.split("\n")[:-1])
    text = text.strip()

    return json.loads(text), None


def main():
    # Step 1: Run extract to get unclassified contacts
    result = subprocess.run(
        [sys.executable, os.path.join(SCRIPT_DIR, "classify_leads.py"), "extract"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        log(f"ERROR extract failed: {result.stderr.strip()}")
        return

    data = json.loads(result.stdout)
    contacts = data.get("contacts", [])
    if not contacts:
        log("OK no unclassified contacts")
        return

    log(f"START classifying {len(contacts)} contacts")

    if not os.path.exists(CLAUDE_BIN):
        log(f"ERROR claude CLI not found at {CLAUDE_BIN}")
        sys.exit(1)

    prompt = data["classification_prompt"]
    classified = 0

    for contact in contacts:
        phone = contact["phone"]
        name = contact.get("name") or phone

        try:
            classification, err = classify_contact(prompt, contact)
            if err:
                log(f"  ERROR classify {phone}: {err}")
                continue

            normalized = classify_leads.normalize_classification_payload(classification)
            stage = normalized["stage"]
            confidence = normalized["confidence"]
            reason = normalized["reason"]
            quality = normalized["quality"]
            proposed_change = normalized["proposed_change"]

            # Step 3: Apply classification
            apply_cmd = [
                sys.executable,
                os.path.join(SCRIPT_DIR, "classify_leads.py"),
                "apply",
                "--phone", phone,
                "--stage", stage,
                "--confidence", str(confidence),
                "--reason", reason,
                "--warmth", str(quality["warmth"]),
                "--timing", str(quality["timing"]),
                "--relevance", str(quality["relevance"]),
                "--trust", str(quality["trust"]),
                "--conversion-readiness", str(quality["conversion_readiness"]),
            ]
            if proposed_change:
                apply_cmd.extend(["--proposed-change", proposed_change])

            apply_result = subprocess.run(
                apply_cmd,
                capture_output=True, text=True
            )
            if apply_result.returncode == 0:
                classified += 1
                log(
                    "  "
                    f"{name}: {stage} (confidence={confidence}; "
                    f"quality={quality})"
                )
            else:
                log(f"  ERROR apply {phone}: {apply_result.stderr.strip()}")

        except (json.JSONDecodeError, KeyError, ValueError) as e:
            log(f"  ERROR parse {phone}: {e}")
        except subprocess.TimeoutExpired:
            log(f"  ERROR timeout {phone}")
        except Exception as e:
            log(f"  ERROR {phone}: {e}")

    log(f"DONE classified {classified}/{len(contacts)} contacts")


if __name__ == "__main__":
    main()
