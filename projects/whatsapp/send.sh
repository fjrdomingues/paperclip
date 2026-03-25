#!/bin/bash
set -euo pipefail

# Send a WhatsApp message via Twilio
# Usage: ./send.sh <to_number> <message>
#        ./send.sh --template <to_number> <content_sid> [var1=val1 var2=val2 ...]
# Example: ./send.sh +351911528501 "Hello from Remodelar AI!"
# Template: ./send.sh --template +351911528501 HX7f566... 1=Fábio

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$(dirname "$SCRIPT_DIR")/telegram/.env"

if [ -f "$ENV_FILE" ]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

: "${TWILIO_ACCOUNT_SID:?Set TWILIO_ACCOUNT_SID}"
: "${TWILIO_API_KEY_SID:?Set TWILIO_API_KEY_SID}"
: "${TWILIO_API_KEY_SECRET:?Set TWILIO_API_KEY_SECRET}"
: "${TWILIO_WHATSAPP_FROM:?Set TWILIO_WHATSAPP_FROM}"

API_URL="https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}/Messages.json"
AUTH="${TWILIO_API_KEY_SID}:${TWILIO_API_KEY_SECRET}"

ensure_whatsapp_prefix() {
  local num="$1"
  if [[ "$num" != whatsapp:* ]]; then
    echo "whatsapp:${num}"
  else
    echo "$num"
  fi
}

send_freeform() {
  local to="$1" message="$2"
  to="$(ensure_whatsapp_prefix "$to")"

  curl -s -X POST "$API_URL" \
    -u "$AUTH" \
    --data-urlencode "From=${TWILIO_WHATSAPP_FROM}" \
    --data-urlencode "To=${to}" \
    --data-urlencode "Body=${message}"
}

send_template() {
  local to="$1" content_sid="$2"
  shift 2
  to="$(ensure_whatsapp_prefix "$to")"

  # Build content variables JSON from key=value args
  local vars="{}"
  for arg in "$@"; do
    local key="${arg%%=*}" val="${arg#*=}"
    vars=$(echo "$vars" | jq --arg k "$key" --arg v "$val" '. + {($k): $v}')
  done

  curl -s -X POST "$API_URL" \
    -u "$AUTH" \
    --data-urlencode "From=${TWILIO_WHATSAPP_FROM}" \
    --data-urlencode "To=${to}" \
    --data-urlencode "ContentSid=${content_sid}" \
    --data-urlencode "ContentVariables=${vars}"
}

# Parse args
if [ "${1:-}" = "--template" ]; then
  shift
  TO="${1:?Usage: send.sh --template <to> <content_sid> [var=val ...]}"
  CONTENT_SID="${2:?Missing content SID}"
  shift 2
  RESPONSE=$(send_template "$TO" "$CONTENT_SID" "$@")
else
  TO="${1:?Usage: send.sh <to_number> <message>}"
  MESSAGE="${2:?Usage: send.sh <to_number> <message>}"
  RESPONSE=$(send_freeform "$TO" "$MESSAGE")
fi

STATUS=$(echo "$RESPONSE" | jq -r '.status // "error"')
SID=$(echo "$RESPONSE" | jq -r '.sid // "none"')
ERROR=$(echo "$RESPONSE" | jq -r '.error_code // "none"')

if [ "$STATUS" = "queued" ] || [ "$STATUS" = "sent" ]; then
  echo "OK: Message $SID status=$STATUS"
else
  echo "FAILED: status=$STATUS error=$ERROR"
  echo "$RESPONSE" | jq -r '.message // empty'
  exit 1
fi
