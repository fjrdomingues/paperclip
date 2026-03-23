#!/bin/bash
set -euo pipefail

# Telegram Cron Poller
# Polls Telegram for new messages and stores them locally as JSONL.
# Designed to run every 60s via system crontab — no Paperclip dependency.
#
# /wake command: if Fábio sends "/wake [context]", the CEO agent heartbeat
# is triggered immediately via paperclipai heartbeat run.
# Requires PAPERCLIP_CEO_API_KEY in .env (one-time setup):
#   paperclipai agent local-cli ceo -C <company-id> --no-install-skills --key-name telegram-wake
# Then append the printed PAPERCLIP_API_KEY value to .env as PAPERCLIP_CEO_API_KEY=<value>

CEO_AGENT_ID="e2b797d0-8f0c-4bcf-adf9-99fd095ea14b"
CEO_ALERT_ISSUE_ID="0d1502be-da96-4db1-bfe4-de78c19e473a"  # WIN-28: CEO alert inbox
PAPERCLIP_API_BASE="${PAPERCLIP_API_BASE:-http://localhost:3100}"
OWNER_CHAT_ID="528866003"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="$SCRIPT_DIR/data"
STATE_FILE="$DATA_DIR/state.json"
INBOX_FILE="$DATA_DIR/inbox.jsonl"
LOG_FILE="$DATA_DIR/poll.log"
ENV_FILE="$SCRIPT_DIR/.env"

# Load env from .env file if present
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
  echo "$(date -Iseconds) ERROR: TELEGRAM_BOT_TOKEN not set" >> "$LOG_FILE"
  exit 1
fi

TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-528866003}"

mkdir -p "$DATA_DIR"

# --- State management ---

load_offset() {
  if [ -f "$STATE_FILE" ]; then
    jq -r '.last_update_id // 0' "$STATE_FILE"
  else
    echo "0"
  fi
}

save_offset() {
  local offset="$1"
  local tmp
  tmp="$(mktemp)"
  jq -n --argjson offset "$offset" '{"last_update_id": $offset, "last_poll": (now | todate)}' > "$tmp"
  mv "$tmp" "$STATE_FILE"
}

sanitize_text() {
  printf '%s' "$1" | sed -E 's/sk-[[:alnum:]_-]{20,}/[REDACTED]/g'
}

# --- /wake command ---

wake_ceo() {
  local context="$1"

  if [ -z "${PAPERCLIP_CEO_API_KEY:-}" ]; then
    echo "$(date -Iseconds) WARN: PAPERCLIP_CEO_API_KEY not set — /wake ignored" >> "$LOG_FILE"
    curl -fsS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      -d "chat_id=${OWNER_CHAT_ID}" \
      --data-urlencode "text=⚠️ /wake not configured. Set PAPERCLIP_CEO_API_KEY in .env." \
      >> "$LOG_FILE" 2>&1 || true
    return
  fi

  # Write wake entry to inbox so CEO sees context on triggered heartbeat
  jq -nc \
    --arg sender_name "System" \
    --argjson sender_id 0 \
    --argjson timestamp "$(date +%s)" \
    --arg type "wake" \
    --arg content "/wake${context:+ }${context}" \
    --arg voice_file_id "" \
    --arg read "false" \
    '{sender_name: $sender_name, sender_id: $sender_id, timestamp: $timestamp, type: $type, content: $content, voice_file_id: $voice_file_id, read: $read}' \
    >> "$INBOX_FILE"

  # Trigger CEO heartbeat via @-mention on the CEO alert inbox issue (WIN-28).
  # The @CEO mention wakes the agent immediately without requiring CEO credentials.
  local comment_body="@Chief /wake from Fabio"
  if [ -n "$context" ]; then
    comment_body="${comment_body}: ${context}"
  fi
  curl -fsS -X POST "${PAPERCLIP_API_BASE}/api/issues/${CEO_ALERT_ISSUE_ID}/comments" \
    -H "Authorization: Bearer ${PAPERCLIP_CEO_API_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"body\":$(printf '%s' "$comment_body" | jq -Rs .)}" \
    -o /dev/null || {
      echo "$(date -Iseconds) ERROR: Failed to post @CEO wake comment" >> "$LOG_FILE"
    }

  local reply="✓ Waking CEO now"
  if [ -n "$context" ]; then
    reply="${reply} — context: ${context}"
  fi
  reply="${reply}."
  curl -fsS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${OWNER_CHAT_ID}" \
    --data-urlencode "text=${reply}" \
    -o /dev/null || true

  echo "$(date -Iseconds) INFO: CEO wake triggered (context: ${context:-none})" >> "$LOG_FILE"
}

# --- Main poll ---

LAST_UPDATE_ID="$(load_offset)"
NEXT_OFFSET=$((LAST_UPDATE_ID + 1))

RESPONSE="$(curl -fsS --max-time 30 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates?offset=${NEXT_OFFSET}&timeout=5" 2>>"$LOG_FILE")" || {
  echo "$(date -Iseconds) ERROR: getUpdates failed" >> "$LOG_FILE"
  exit 1
}

OK="$(printf '%s' "$RESPONSE" | jq -r '.ok')"
if [ "$OK" != "true" ]; then
  echo "$(date -Iseconds) ERROR: Telegram returned ok=false" >> "$LOG_FILE"
  exit 1
fi

COUNT="$(printf '%s' "$RESPONSE" | jq -r '.result | length')"
if [ "$COUNT" -eq 0 ]; then
  save_offset "$LAST_UPDATE_ID"
  exit 0
fi

HIGHEST="$(printf '%s' "$RESPONSE" | jq -r '.result | max_by(.update_id) | .update_id')"

# Append each message to inbox.jsonl
while IFS= read -r UPDATE; do
  MESSAGE="$(printf '%s' "$UPDATE" | jq -c '.message // empty')"
  if [ -z "$MESSAGE" ] || [ "$MESSAGE" = "null" ]; then
    continue
  fi

  SENDER_NAME="$(printf '%s' "$MESSAGE" | jq -r '(.from.first_name // "") + " " + (.from.last_name // "")' | xargs)"
  SENDER_ID="$(printf '%s' "$MESSAGE" | jq -r '.from.id')"
  TIMESTAMP="$(printf '%s' "$MESSAGE" | jq -r '.date')"
  TEXT="$(printf '%s' "$MESSAGE" | jq -r '.text // empty')"
  VOICE_DURATION="$(printf '%s' "$MESSAGE" | jq -r '.voice.duration // empty')"
  VOICE_FILE_ID="$(printf '%s' "$MESSAGE" | jq -r '.voice.file_id // empty')"
  DOC_NAME="$(printf '%s' "$MESSAGE" | jq -r '.document.file_name // empty')"
  HAS_PHOTO="$(printf '%s' "$MESSAGE" | jq -r 'if .photo then "yes" else "" end')"
  HAS_VIDEO="$(printf '%s' "$MESSAGE" | jq -r 'if .video then "yes" else "" end')"

  # Determine message type and content
  MSG_TYPE="text"
  MSG_CONTENT=""
  if [ -n "$TEXT" ]; then
    MSG_CONTENT="$(sanitize_text "$TEXT")"
  elif [ -n "$VOICE_DURATION" ]; then
    MSG_TYPE="voice"
    MSG_CONTENT="[Voice message, ${VOICE_DURATION}s]"
    # Transcription handled by CEO skill on-demand
  elif [ -n "$DOC_NAME" ]; then
    MSG_TYPE="document"
    MSG_CONTENT="[Document: $DOC_NAME]"
  elif [ -n "$HAS_PHOTO" ]; then
    MSG_TYPE="photo"
    MSG_CONTENT="[Photo]"
  elif [ -n "$HAS_VIDEO" ]; then
    MSG_TYPE="video"
    MSG_CONTENT="[Video]"
  else
    MSG_TYPE="unknown"
    MSG_CONTENT="[Unsupported message type]"
  fi

  # Detect /wake command from the owner
  if [ "$SENDER_ID" = "$OWNER_CHAT_ID" ] && printf '%s' "$TEXT" | grep -qiE '^/wake'; then
    WAKE_CONTEXT="$(printf '%s' "$TEXT" | sed -E 's|^/wake[[:space:]]*||i')"
    wake_ceo "$WAKE_CONTEXT"
  fi

  # Write to inbox as JSONL
  jq -nc \
    --arg sender_name "$SENDER_NAME" \
    --argjson sender_id "$SENDER_ID" \
    --argjson timestamp "$TIMESTAMP" \
    --arg type "$MSG_TYPE" \
    --arg content "$MSG_CONTENT" \
    --arg voice_file_id "${VOICE_FILE_ID:-}" \
    --arg read "false" \
    '{sender_name: $sender_name, sender_id: $sender_id, timestamp: $timestamp, type: $type, content: $content, voice_file_id: $voice_file_id, read: $read}' \
    >> "$INBOX_FILE"

done < <(printf '%s' "$RESPONSE" | jq -c '.result[]')

save_offset "$HIGHEST"
echo "$(date -Iseconds) OK: $COUNT messages ingested (offset=$HIGHEST)" >> "$LOG_FILE"
