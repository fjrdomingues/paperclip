#!/bin/bash
# Ensure Homebrew binaries (jq, etc.) are on PATH when run from launchd/osascript
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
set -euo pipefail

# Telegram Cron Poller
# Polls Telegram for new messages and stores them locally as JSONL.
# Designed to run every 60s via launchd with a local Paperclip CLI available.
#
# Auto-wake: any message from Fábio triggers a CEO heartbeat automatically.
# The primary path invokes Chief directly via `paperclipai heartbeat run`.
# A short cooldown only guards against back-to-back duplicate launches; each new
# poll batch should still wake promptly.
#
# /wake command: "/wake [context]" always triggers immediately (ignores cooldown).
#
# Requires PAPERCLIP_CEO_API_KEY in .env (one-time setup):
#   paperclipai agent local-cli ceo -C <company-id> --no-install-skills --key-name telegram-wake
# Then append the printed PAPERCLIP_API_KEY value to .env as PAPERCLIP_CEO_API_KEY=<value>

CEO_AGENT_ID="e2b797d0-8f0c-4bcf-adf9-99fd095ea14b"
CEO_ALERT_ISSUE_ID="0d1502be-da96-4db1-bfe4-de78c19e473a"  # WIN-28: degraded fallback only
PAPERCLIP_API_BASE="${PAPERCLIP_API_BASE:-http://localhost:3100}"
OWNER_CHAT_ID="528866003"
AUTO_WAKE_COOLDOWN_SEC="${AUTO_WAKE_COOLDOWN_SEC:-45}"
CEO_HEARTBEAT_TIMEOUT_MS="${CEO_HEARTBEAT_TIMEOUT_MS:-180000}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="$SCRIPT_DIR/data"
STATE_FILE="$DATA_DIR/state.json"
INBOX_FILE="$DATA_DIR/inbox.jsonl"
LOG_FILE="$DATA_DIR/poll.log"
HEARTBEAT_LOG_FILE="$DATA_DIR/heartbeat.log"
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

# --- Log rotation ---
# Trim log files to the last LOG_MAX_LINES lines to prevent unbounded growth.
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
rotate_log "$HEARTBEAT_LOG_FILE"
rotate_log "$LAUNCHD_LOG_DIR/telegram-poll-stdout.log"
rotate_log "$LAUNCHD_LOG_DIR/telegram-poll-stderr.log"

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
  # Preserve last_auto_wake from existing state when saving offset
  local existing_wake
  existing_wake="$(jq -r '.last_auto_wake // 0' "$STATE_FILE" 2>/dev/null || echo 0)"
  jq -n --argjson offset "$offset" --argjson last_auto_wake "$existing_wake" \
    '{"last_update_id": $offset, "last_poll": (now | todate), "last_auto_wake": $last_auto_wake}' > "$tmp"
  mv "$tmp" "$STATE_FILE"
}

save_auto_wake() {
  local tmp
  tmp="$(mktemp)"
  jq -n \
    --argjson offset "$(load_offset)" \
    --argjson ts "$(date +%s)" \
    '{"last_update_id": $offset, "last_poll": (now | todate), "last_auto_wake": $ts}' > "$tmp"
  if [ -f "$STATE_FILE" ]; then
    jq --argjson ts "$(date +%s)" '.last_auto_wake = $ts' "$STATE_FILE" > "$tmp"
  fi
  mv "$tmp" "$STATE_FILE"
}

should_auto_wake() {
  local last_wake
  last_wake="$(jq -r '.last_auto_wake // 0' "$STATE_FILE" 2>/dev/null || echo 0)"
  local now
  now="$(date +%s)"
  local elapsed=$(( now - last_wake ))
  [ "$elapsed" -ge "$AUTO_WAKE_COOLDOWN_SEC" ]
}

sanitize_text() {
  printf '%s' "$1" | sed -E 's/sk-[[:alnum:]_-]{20,}/[REDACTED]/g'
}

# --- Document download helper ---
# Usage: download_telegram_document <file_id> [filename]
# Downloads a Telegram document by file_id and prints the local path.
# Caller is responsible for removing the temp dir: rm -rf "$(dirname "$output_path")"
download_telegram_document() {
  local file_id="$1"
  local filename="${2:-document}"
  local metadata
  local file_path
  local tmp_dir
  local output_path

  metadata="$(curl -fsS --max-time 30 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getFile?file_id=${file_id}" 2>>"$LOG_FILE")"
  file_path="$(printf '%s' "$metadata" | jq -r '.result.file_path // empty')"

  if [ -z "$file_path" ]; then
    echo "$(date -Iseconds) ERROR: getFile failed for file_id=$file_id" >> "$LOG_FILE"
    return 1
  fi

  tmp_dir="$(mktemp -d)"
  output_path="$tmp_dir/$filename"
  curl -fsS --max-time 60 "https://api.telegram.org/file/bot${TELEGRAM_BOT_TOKEN}/${file_path}" -o "$output_path" 2>>"$LOG_FILE"
  printf '%s' "$output_path"
}

# --- /wake command ---

fallback_issue_wake() {
  local context="$1"
  local comment_body="@Chief Telegram wake fallback"

  if [ -n "$context" ]; then
    comment_body="${comment_body}: ${context}"
  fi

  if curl -fsS -X POST "${PAPERCLIP_API_BASE}/api/issues/${CEO_ALERT_ISSUE_ID}/comments" \
    -H "Authorization: Bearer ${PAPERCLIP_CEO_API_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"body\":$(printf '%s' "$comment_body" | jq -Rs .)}" \
    -o /dev/null; then
    echo "$(date -Iseconds) WARN: Direct CEO wake failed; fell back to WIN-28 comment transport" >> "$LOG_FILE"
    return 0
  fi

  echo "$(date -Iseconds) ERROR: Fallback CEO wake comment also failed" >> "$LOG_FILE"
  return 1
}

invoke_ceo_heartbeat() {
  local context="$1"
  local -a cmd=(
    paperclipai heartbeat run
    --agent-id "$CEO_AGENT_ID"
    --api-base "$PAPERCLIP_API_BASE"
    --api-key "$PAPERCLIP_CEO_API_KEY"
    --source automation
    --trigger callback
    --timeout-ms "$CEO_HEARTBEAT_TIMEOUT_MS"
    --json
  )
  local pid
  local status

  if ! command -v paperclipai >/dev/null 2>&1; then
    echo "$(date -Iseconds) ERROR: paperclipai CLI not found; cannot invoke CEO directly" >> "$LOG_FILE"
    fallback_issue_wake "$context"
    return 1
  fi

  {
    printf '%s INFO: Starting CEO heartbeat:' "$(date -Iseconds)"
    printf ' %q' "${cmd[@]}"
    if [ -n "$context" ]; then
      printf ' # context=%s' "$context"
    fi
    printf '\n'
  } >> "$HEARTBEAT_LOG_FILE"

  nohup "${cmd[@]}" >> "$HEARTBEAT_LOG_FILE" 2>&1 &
  pid=$!

  sleep 2
  if kill -0 "$pid" 2>/dev/null; then
    echo "$(date -Iseconds) INFO: CEO heartbeat launched directly (pid=$pid, context: ${context:-none})" >> "$LOG_FILE"
    return 0
  fi

  wait "$pid"
  status=$?
  if [ "$status" -eq 0 ]; then
    echo "$(date -Iseconds) INFO: CEO heartbeat completed immediately (context: ${context:-none})" >> "$LOG_FILE"
    return 0
  fi

  echo "$(date -Iseconds) ERROR: Direct CEO heartbeat exited immediately (status=$status)" >> "$LOG_FILE"
  fallback_issue_wake "$context"
  return 1
}

wake_ceo() {
  local context="$1"
  local notify_owner="${2:-0}"

  if [ -z "${PAPERCLIP_CEO_API_KEY:-}" ]; then
    echo "$(date -Iseconds) WARN: PAPERCLIP_CEO_API_KEY not set — /wake ignored" >> "$LOG_FILE"
    if [ "$notify_owner" = "1" ]; then
      curl -fsS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${OWNER_CHAT_ID}" \
        --data-urlencode "text=⚠️ /wake not configured. Set PAPERCLIP_CEO_API_KEY in .env." \
        >> "$LOG_FILE" 2>&1 || true
    fi
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
    --arg document_file_id "" \
    --arg read "false" \
    '{sender_name: $sender_name, sender_id: $sender_id, timestamp: $timestamp, type: $type, content: $content, voice_file_id: $voice_file_id, document_file_id: $document_file_id, read: $read}' \
    >> "$INBOX_FILE"

  invoke_ceo_heartbeat "$context"

  if [ "$notify_owner" = "1" ]; then
    local reply="✓ Waking CEO now"
    if [ -n "$context" ]; then
      reply="${reply} — context: ${context}"
    fi
    reply="${reply}."
    curl -fsS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
      -d "chat_id=${OWNER_CHAT_ID}" \
      --data-urlencode "text=${reply}" \
      -o /dev/null || true
  fi

  echo "$(date -Iseconds) INFO: CEO wake triggered (context: ${context:-none})" >> "$LOG_FILE"
}

# --- Main poll ---

LAST_UPDATE_ID="$(load_offset)"
NEXT_OFFSET=$((LAST_UPDATE_ID + 1))

RESPONSE=""
for _attempt in 1 2 3; do
  RESPONSE="$(curl -fsS --max-time 35 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates?offset=${NEXT_OFFSET}&timeout=5" 2>>"$LOG_FILE")" && break
  echo "$(date -Iseconds) WARN: getUpdates attempt ${_attempt}/3 failed, retrying in 3s..." >> "$LOG_FILE"
  sleep 3
done
if [ -z "$RESPONSE" ]; then
  echo "$(date -Iseconds) WARN: getUpdates failed after 3 attempts (transient, will retry next cycle)" >> "$LOG_FILE"
  exit 0
fi

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

OWNER_MSG_COUNT=0  # Track owner messages for auto-wake
WAKE_ALREADY_FIRED=0  # Track if explicit /wake was used this cycle

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
  DOC_FILE_ID="$(printf '%s' "$MESSAGE" | jq -r '.document.file_id // empty')"
  PHOTO_FILE_ID="$(printf '%s' "$MESSAGE" | jq -r 'if .photo then (.photo | sort_by(.file_size) | last | .file_id) else "" end')"
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
  elif [ -n "$PHOTO_FILE_ID" ]; then
    MSG_TYPE="photo"
    MSG_CONTENT="[Photo]"
  elif [ -n "$HAS_VIDEO" ]; then
    MSG_TYPE="video"
    MSG_CONTENT="[Video]"
  else
    MSG_TYPE="unknown"
    MSG_CONTENT="[Unsupported message type]"
  fi

  # Track messages from the owner for auto-wake
  if [ "$SENDER_ID" = "$OWNER_CHAT_ID" ]; then
    OWNER_MSG_COUNT=$((OWNER_MSG_COUNT + 1))
  fi

  # Detect /wake command from the owner (explicit, always fires regardless of cooldown)
  if [ "$SENDER_ID" = "$OWNER_CHAT_ID" ] && printf '%s' "$TEXT" | grep -qiE '^/wake'; then
    WAKE_CONTEXT="$(printf '%s' "$TEXT" | sed -E 's|^/wake[[:space:]]*||i')"
    wake_ceo "$WAKE_CONTEXT" 1
    save_auto_wake  # Reset cooldown so auto-wake doesn't double-fire
    WAKE_ALREADY_FIRED=1
  fi

  # Write to inbox as JSONL
  jq -nc \
    --arg sender_name "$SENDER_NAME" \
    --argjson sender_id "$SENDER_ID" \
    --argjson timestamp "$TIMESTAMP" \
    --arg type "$MSG_TYPE" \
    --arg content "$MSG_CONTENT" \
    --arg voice_file_id "${VOICE_FILE_ID:-}" \
    --arg document_file_id "${DOC_FILE_ID:-}" \
    --arg photo_file_id "${PHOTO_FILE_ID:-}" \
    --arg read "false" \
    '{sender_name: $sender_name, sender_id: $sender_id, timestamp: $timestamp, type: $type, content: $content, voice_file_id: $voice_file_id, document_file_id: $document_file_id, photo_file_id: $photo_file_id, read: $read}' \
    >> "$INBOX_FILE"

done < <(printf '%s' "$RESPONSE" | jq -c '.result[]')

save_offset "$HIGHEST"
echo "$(date -Iseconds) OK: $COUNT messages ingested (offset=$HIGHEST)" >> "$LOG_FILE"

# Auto-wake CEO on any owner message (with cooldown, skip if /wake already fired)
if [ "$OWNER_MSG_COUNT" -gt 0 ] && [ "$WAKE_ALREADY_FIRED" -eq 0 ]; then
  if should_auto_wake; then
    echo "$(date -Iseconds) INFO: Auto-waking CEO ($OWNER_MSG_COUNT owner message(s))" >> "$LOG_FILE"
    wake_ceo "New Telegram message(s) from Fábio" 0
    save_auto_wake
  else
    echo "$(date -Iseconds) INFO: Skipping CEO auto-wake due to short cooldown (${AUTO_WAKE_COOLDOWN_SEC}s)" >> "$LOG_FILE"
  fi
fi
