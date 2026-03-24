#!/bin/bash
# Ensure Homebrew binaries (jq, etc.) are on PATH when run from launchd/osascript
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
set -euo pipefail

# WhatsApp Inbound Message Poller
# Polls Twilio Messages API for inbound WhatsApp messages and stores them as JSONL.
# Designed to run every 60s via launchd. No Paperclip dependency.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="$SCRIPT_DIR/data"
STATE_FILE="$DATA_DIR/state.json"
INBOX_FILE="$DATA_DIR/inbox.jsonl"
LOG_FILE="$DATA_DIR/poll.log"

mkdir -p "$DATA_DIR"

# Load Twilio credentials from telegram/.env
ENV_FILE="$SCRIPT_DIR/../telegram/.env"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

if [ -z "${TWILIO_ACCOUNT_SID:-}" ] || [ -z "${TWILIO_API_KEY_SID:-}" ] || [ -z "${TWILIO_API_KEY_SECRET:-}" ]; then
  echo "$(date -Iseconds) ERROR: Twilio credentials not set (TWILIO_ACCOUNT_SID, TWILIO_API_KEY_SID, TWILIO_API_KEY_SECRET)" >> "$LOG_FILE"
  exit 1
fi

WHATSAPP_TO="${TWILIO_WHATSAPP_FROM:-whatsapp:+15559382429}"

# --- Log rotation ---
LOG_MAX_LINES=1000
LAUNCHD_LOG_DIR="$HOME/.paperclip/logs"

rotate_log() {
  local log_file="$1"
  if [ -f "$log_file" ]; then
    local line_count
    line_count="$(wc -l < "$log_file")"
    if [ "$line_count" -gt "$LOG_MAX_LINES" ]; then
      local tmp
      tmp="$(mktemp)"
      tail -n "$LOG_MAX_LINES" "$log_file" > "$tmp" && mv "$tmp" "$log_file"
    fi
  fi
}

rotate_log "$LOG_FILE"
rotate_log "$LAUNCHD_LOG_DIR/whatsapp-poll-stdout.log"
rotate_log "$LAUNCHD_LOG_DIR/whatsapp-poll-stderr.log"

# --- State management ---

load_state() {
  if [ -f "$STATE_FILE" ]; then
    LAST_SID="$(jq -r '.last_message_sid // ""' "$STATE_FILE")"
    LAST_POLL="$(jq -r '.last_poll // ""' "$STATE_FILE")"
  else
    LAST_SID=""
    LAST_POLL=""
  fi
}

save_state() {
  local sid="$1"
  local tmp
  tmp="$(mktemp)"
  jq -n \
    --arg last_message_sid "$sid" \
    --arg last_poll "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
    '{"last_message_sid": $last_message_sid, "last_poll": $last_poll}' > "$tmp"
  mv "$tmp" "$STATE_FILE"
}

# Convert Twilio RFC 2822 date string to epoch
# Example: "Wed, 24 Mar 2026 23:00:00 +0000"
twilio_date_to_epoch() {
  date -jf "%a, %d %b %Y %H:%M:%S %z" "$1" "+%s" 2>/dev/null || echo 0
}

# Convert ISO-8601 UTC string to epoch
iso_to_epoch() {
  date -jf "%Y-%m-%dT%H:%M:%SZ" "$1" "+%s" 2>/dev/null || echo 0
}

# --- Main ---

load_state

# Determine DateSent filter for Twilio API
if [ -z "$LAST_POLL" ]; then
  # First run: only capture messages from the last hour
  DATE_FILTER="$(date -u -v-1H '+%Y-%m-%d')"
  LAST_POLL_EPOCH=$(( $(date -u '+%s') - 3600 ))
else
  # Subsequent runs: use date portion of last_poll (date-only filter)
  DATE_FILTER="$(echo "$LAST_POLL" | cut -c1-10)"
  LAST_POLL_EPOCH="$(iso_to_epoch "$LAST_POLL")"
fi

# Poll Twilio Messages API
RESPONSE="$(curl -fsS --max-time 30 \
  -u "${TWILIO_API_KEY_SID}:${TWILIO_API_KEY_SECRET}" \
  "https://api.twilio.com/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}/Messages.json?To=${WHATSAPP_TO}&PageSize=50&DateSent>=${DATE_FILTER}" \
  2>>"$LOG_FILE")" || {
  echo "$(date -Iseconds) ERROR: Twilio API request failed" >> "$LOG_FILE"
  exit 1
}

# Validate response
if ! echo "$RESPONSE" | jq -e '.messages' > /dev/null 2>&1; then
  echo "$(date -Iseconds) ERROR: Unexpected Twilio response: $(echo "$RESPONSE" | head -c 200)" >> "$LOG_FILE"
  exit 1
fi

TOTAL="$(echo "$RESPONSE" | jq '.messages | length')"
NEW_COUNT=0
NEWEST_SID="$LAST_SID"
NEWEST_EPOCH=0

# Process inbound messages
while IFS= read -r MSG; do
  [ -z "$MSG" ] && continue

  DIRECTION="$(echo "$MSG" | jq -r '.direction')"
  [ "$DIRECTION" != "inbound" ] && continue

  SID="$(echo "$MSG" | jq -r '.sid')"
  DATE_SENT_STR="$(echo "$MSG" | jq -r '.date_sent')"
  MSG_EPOCH="$(twilio_date_to_epoch "$DATE_SENT_STR")"

  # Skip messages at or before last poll time
  if [ "$MSG_EPOCH" -le "$LAST_POLL_EPOCH" ]; then
    continue
  fi

  # Skip if same SID as last processed (dedup for messages at exact last_poll boundary)
  if [ "$SID" = "$LAST_SID" ]; then
    continue
  fi

  FROM="$(echo "$MSG" | jq -r '.from' | sed 's/whatsapp://')"
  BODY="$(echo "$MSG" | jq -r '.body // ""')"
  # Convert Twilio RFC 2822 to ISO-8601
  TIMESTAMP="$(date -jf "%a, %d %b %Y %H:%M:%S %z" "$DATE_SENT_STR" "+%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "$DATE_SENT_STR")"

  jq -nc \
    --arg from "$FROM" \
    --arg body "$BODY" \
    --arg timestamp "$TIMESTAMP" \
    --arg sid "$SID" \
    --arg status "received" \
    '{from: $from, body: $body, timestamp: $timestamp, sid: $sid, status: $status}' \
    >> "$INBOX_FILE"

  NEW_COUNT=$((NEW_COUNT + 1))

  # Track newest message for state update
  if [ "$MSG_EPOCH" -gt "$NEWEST_EPOCH" ]; then
    NEWEST_EPOCH="$MSG_EPOCH"
    NEWEST_SID="$SID"
  fi

done < <(echo "$RESPONSE" | jq -c '.messages[]')

# Save state (update SID if we saw new messages)
save_state "$NEWEST_SID"

if [ "$NEW_COUNT" -gt 0 ]; then
  echo "$(date -Iseconds) OK: $NEW_COUNT new inbound message(s) (${TOTAL} total fetched, newest SID=$NEWEST_SID)" >> "$LOG_FILE"
else
  echo "$(date -Iseconds) OK: no new messages (${TOTAL} total fetched)" >> "$LOG_FILE"
fi
