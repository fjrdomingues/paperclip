#!/bin/bash
# Ensure Homebrew binaries (jq, etc.) are on PATH when run from launchd/osascript
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
set -euo pipefail

# WhatsApp Inbound Message Poller
# Polls Twilio Messages API for inbound WhatsApp messages and stores them as JSONL.
# Designed to run every 60s via launchd. No Paperclip dependency.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="${WHATSAPP_POLL_DATA_DIR:-$SCRIPT_DIR/data}"
STATE_FILE="${WHATSAPP_POLL_STATE_FILE:-$DATA_DIR/state.json}"
INBOX_FILE="${WHATSAPP_POLL_INBOX_FILE:-$DATA_DIR/inbox.jsonl}"
LOG_FILE="${WHATSAPP_POLL_LOG_FILE:-$DATA_DIR/poll.log}"
MOCK_RESPONSE_FILE="${WHATSAPP_POLL_MOCK_RESPONSE_FILE:-}"
SEEN_SIDS_FILE="$(mktemp)"
trap 'rm -f "$SEEN_SIDS_FILE"' EXIT

mkdir -p "$DATA_DIR"

# Load Twilio credentials from telegram/.env
ENV_FILE="${WHATSAPP_POLL_ENV_FILE:-$SCRIPT_DIR/../telegram/.env}"
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

WHATSAPP_TO="${TWILIO_WHATSAPP_FROM:-whatsapp:+351912508220}"
# URL-encode for Twilio API query parameter (: → %3A, + → %2B)
WHATSAPP_TO_ENCODED="$(echo "$WHATSAPP_TO" | sed 's/:/%3A/g; s/+/%2B/g')"
TWILIO_API_BASE="https://api.twilio.com"
MAX_RECOVERY_PAGES="${WHATSAPP_POLL_MAX_RECOVERY_PAGES:-100}"

# --- Log rotation ---
LOG_MAX_LINES=1000
LAUNCHD_LOG_DIR="${WHATSAPP_POLL_LAUNCHD_LOG_DIR:-$HOME/.paperclip/logs}"

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
  local last_poll="$2"
  local last_run
  last_run="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  local tmp
  tmp="$(mktemp)"
  jq -n \
    --arg last_message_sid "$sid" \
    --arg last_poll "$last_poll" \
    --arg last_run "$last_run" \
    '{"last_message_sid": $last_message_sid, "last_poll": $last_poll, "last_run": $last_run}' > "$tmp"
  mv "$tmp" "$STATE_FILE"
}

# Convert Twilio RFC 2822 date string to epoch
# Example: "Wed, 24 Mar 2026 23:00:00 +0000"
twilio_date_to_epoch() {
  date -jf "%a, %d %b %Y %H:%M:%S %z" "$1" "+%s" 2>/dev/null || echo 0
}

# Convert ISO-8601 UTC string to epoch
iso_to_epoch() {
  date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$1" "+%s" 2>/dev/null || echo 0
}

init_seen_sids() {
  : > "$SEEN_SIDS_FILE"
  if [ -f "$INBOX_FILE" ]; then
    jq -r '.sid? // empty' "$INBOX_FILE" 2>/dev/null | sed '/^$/d' | sort -u > "$SEEN_SIDS_FILE" || true
  fi
}

clamp_cursor_to_last_seen_sid() {
  local response="$1"

  if [ -z "$LAST_POLL" ] || [ -z "$LAST_SID" ]; then
    return
  fi

  local last_sid_date_sent
  last_sid_date_sent="$(echo "$response" | jq -r --arg sid "$LAST_SID" '.messages[] | select(.sid == $sid) | .date_sent // empty' | head -n 1)"
  if [ -z "$last_sid_date_sent" ]; then
    return
  fi

  local last_sid_epoch
  last_sid_epoch="$(twilio_date_to_epoch "$last_sid_date_sent")"
  if [ "$last_sid_epoch" -gt 0 ] && [ "$LAST_POLL_EPOCH" -gt "$last_sid_epoch" ]; then
    LAST_POLL="$(date -u -jf "%a, %d %b %Y %H:%M:%S %z" "$last_sid_date_sent" "+%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "$LAST_POLL")"
    LAST_POLL_EPOCH="$last_sid_epoch"
    echo "$(date -Iseconds) INFO: clamped stale cursor to last seen SID timestamp ($LAST_SID @ $LAST_POLL)" >> "$LOG_FILE"
  fi
}

sid_seen() {
  local sid="$1"
  [ -n "$sid" ] && grep -Fxq "$sid" "$SEEN_SIDS_FILE"
}

mark_sid_seen() {
  local sid="$1"
  [ -n "$sid" ] && printf '%s\n' "$sid" >> "$SEEN_SIDS_FILE"
}

fetch_twilio_response() {
  local initial_url
  local page_url
  local page_number=1
  local page_count=0
  local response
  local next_page_url=""
  local aggregate_file

  initial_url="${TWILIO_API_BASE}/2010-04-01/Accounts/${TWILIO_ACCOUNT_SID}/Messages.json?To=${WHATSAPP_TO_ENCODED}&PageSize=50&DateSent%3E%3D${DATE_FILTER}"
  page_url="$initial_url"
  aggregate_file="$(mktemp)"

  while [ -n "$page_url" ]; do
    if [ -n "$MOCK_RESPONSE_FILE" ]; then
      if [ -d "$MOCK_RESPONSE_FILE" ]; then
        local mock_page_file
        mock_page_file="$MOCK_RESPONSE_FILE/page-${page_number}.json"
        if [ ! -f "$mock_page_file" ]; then
          rm -f "$aggregate_file"
          echo "$(date -Iseconds) ERROR: Missing mock Twilio response page $page_number at $mock_page_file" >> "$LOG_FILE"
          return 1
        fi
        response="$(cat "$mock_page_file")"
      else
        response="$(cat "$MOCK_RESPONSE_FILE")"
      fi
    else
      response="$(curl -fsS --max-time 30 \
        -u "${TWILIO_API_KEY_SID}:${TWILIO_API_KEY_SECRET}" \
        "$page_url")"
    fi

    if ! echo "$response" | jq -e '.messages' > /dev/null 2>&1; then
      rm -f "$aggregate_file"
      echo "$response"
      return 0
    fi

    printf '%s\n' "$response" >> "$aggregate_file"
    page_count=$((page_count + 1))

    if [ -z "$LAST_SID" ] || echo "$response" | jq -e --arg sid "$LAST_SID" '.messages[]? | select(.sid == $sid)' > /dev/null 2>&1; then
      break
    fi

    next_page_url="$(echo "$response" | jq -r '.next_page_uri // empty')"
    if [ -z "$next_page_url" ]; then
      break
    fi

    if [ "$page_count" -ge "$MAX_RECOVERY_PAGES" ]; then
      echo "$(date -Iseconds) WARN: stopping Twilio recovery pagination after $page_count pages without finding anchor SID $LAST_SID" >> "$LOG_FILE"
      break
    fi

    case "$next_page_url" in
      http://*|https://*)
        page_url="$next_page_url"
        ;;
      /*)
        page_url="${TWILIO_API_BASE}${next_page_url}"
        ;;
      *)
        page_url="${TWILIO_API_BASE}/${next_page_url}"
        ;;
    esac
    page_number=$((page_number + 1))
  done

  if [ "$page_count" -gt 1 ] && [ -n "$LAST_SID" ]; then
    echo "$(date -Iseconds) INFO: paged $page_count Twilio response(s) while recovering anchor SID $LAST_SID" >> "$LOG_FILE"
  fi

  jq -cs '{messages: [.[].messages[]?]}' "$aggregate_file"
  rm -f "$aggregate_file"
}

# --- Main ---

load_state
init_seen_sids

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
RESPONSE="$(fetch_twilio_response 2>>"$LOG_FILE")" || {
  echo "$(date -Iseconds) ERROR: Twilio API request failed" >> "$LOG_FILE"
  exit 1
}

# Validate response
if ! echo "$RESPONSE" | jq -e '.messages' > /dev/null 2>&1; then
  echo "$(date -Iseconds) ERROR: Unexpected Twilio response: $(echo "$RESPONSE" | head -c 200)" >> "$LOG_FILE"
  exit 1
fi

clamp_cursor_to_last_seen_sid "$RESPONSE"

TOTAL="$(echo "$RESPONSE" | jq '.messages | length')"
NEW_COUNT=0
NEWEST_SID="$LAST_SID"
NEWEST_EPOCH="$LAST_POLL_EPOCH"
NEWEST_TIMESTAMP="$LAST_POLL"

# Process inbound messages
while IFS= read -r MSG; do
  [ -z "$MSG" ] && continue

  DIRECTION="$(echo "$MSG" | jq -r '.direction')"
  [ "$DIRECTION" != "inbound" ] && continue

  SID="$(echo "$MSG" | jq -r '.sid')"
  DATE_SENT_STR="$(echo "$MSG" | jq -r '.date_sent')"
  MSG_EPOCH="$(twilio_date_to_epoch "$DATE_SENT_STR")"

  # Skip messages at or before last poll time
  if [ "$MSG_EPOCH" -lt "$LAST_POLL_EPOCH" ]; then
    continue
  fi

  # Skip if same SID as last processed (dedup for messages at exact last_poll boundary)
  if [ "$MSG_EPOCH" -eq "$LAST_POLL_EPOCH" ] && [ "$SID" = "$LAST_SID" ]; then
    continue
  fi

  if sid_seen "$SID"; then
    continue
  fi

  FROM="$(echo "$MSG" | jq -r '.from' | sed 's/whatsapp://')"
  BODY="$(echo "$MSG" | jq -r '.body // ""')"
  # Convert Twilio RFC 2822 to ISO-8601
  TIMESTAMP="$(date -u -jf "%a, %d %b %Y %H:%M:%S %z" "$DATE_SENT_STR" "+%Y-%m-%dT%H:%M:%SZ" 2>/dev/null || echo "$DATE_SENT_STR")"

  jq -nc \
    --arg from "$FROM" \
    --arg body "$BODY" \
    --arg timestamp "$TIMESTAMP" \
    --arg sid "$SID" \
    --arg status "received" \
    '{from: $from, body: $body, timestamp: $timestamp, sid: $sid, status: $status}' \
    >> "$INBOX_FILE"
  mark_sid_seen "$SID"

  NEW_COUNT=$((NEW_COUNT + 1))

  # Track newest message for state update
  if [ "$MSG_EPOCH" -gt "$NEWEST_EPOCH" ]; then
    NEWEST_EPOCH="$MSG_EPOCH"
    NEWEST_SID="$SID"
    NEWEST_TIMESTAMP="$TIMESTAMP"
  fi

done < <(echo "$RESPONSE" | jq -c '.messages[]')

STATE_CURSOR="$LAST_POLL"
if [ "$NEW_COUNT" -gt 0 ] && [ -n "$NEWEST_TIMESTAMP" ]; then
  STATE_CURSOR="$NEWEST_TIMESTAMP"
fi

# Save state (only advance the cursor when we actually ingest a newer message)
save_state "$NEWEST_SID" "$STATE_CURSOR"

if [ "$NEW_COUNT" -gt 0 ]; then
  echo "$(date -Iseconds) OK: $NEW_COUNT new inbound message(s) (${TOTAL} total fetched, newest SID=$NEWEST_SID)" >> "$LOG_FILE"
else
  echo "$(date -Iseconds) OK: no new messages (${TOTAL} total fetched)" >> "$LOG_FILE"
fi
