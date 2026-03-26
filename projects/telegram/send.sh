#!/usr/bin/env bash
# Telegram send wrapper — sends message AND logs to inbox.jsonl
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/.env"

INBOX="$SCRIPT_DIR/data/inbox.jsonl"

MESSAGE="${1:-}"
SENDER_NAME="${2:-Agent}"

if [ -z "$MESSAGE" ]; then
  echo "Usage: send.sh <message> [sender_name]" >&2
  exit 1
fi

# Send via Telegram API
RESPONSE=$(curl -fsS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  -d "chat_id=${TELEGRAM_CHAT_ID}" \
  --data-urlencode "text=${MESSAGE}")

# Extract timestamp from API response (fall back to current time)
TIMESTAMP=$(echo "$RESPONSE" | jq -r '.result.date // empty' 2>/dev/null || true)
if [ -z "$TIMESTAMP" ]; then
  TIMESTAMP=$(date +%s)
fi

# Append outbound record to inbox.jsonl
jq -nc \
  --arg sender_name "$SENDER_NAME" \
  --argjson sender_id 0 \
  --argjson timestamp "$TIMESTAMP" \
  --arg type "text" \
  --arg content "$MESSAGE" \
  --arg direction "outbound" \
  '{sender_name: $sender_name, sender_id: $sender_id, timestamp: $timestamp, type: $type, content: $content, voice_file_id: "", document_file_id: "", photo_file_id: "", read: "true", direction: $direction}' \
  >> "$INBOX"

echo "$RESPONSE"
