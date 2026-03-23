#!/bin/bash
set -euo pipefail

# Telegram Agent Polling Script
# Polls Telegram for new messages and relays to Paperclip (WIN-12)
# Monitors WIN-12 for CEO "SEND:" messages and relays them to Telegram exactly once

AGENT_HOME_DIR="${AGENT_HOME:-$(cd "$(dirname "$0")" && pwd)}"
MEMORY_FILE="$AGENT_HOME_DIR/memory/telegram_state.json"
ISSUE_ID="dd92a8e7-bba2-42da-adff-20d38f5dee76"
TELEGRAM_CHAT_ID="528866003"
CEO_AGENT_ID="${TELEGRAM_ALLOWED_AUTHOR_AGENT_ID:-e2b797d0-8f0c-4bcf-adf9-99fd095ea14b}"
CHECKED_OUT=0

require_env() {
  local name="$1"
  if [ -z "${!name:-}" ]; then
    echo "Missing required environment variable: $name" >&2
    exit 1
  fi
}

load_state_value() {
  local key="$1"
  local fallback="$2"
  if [ ! -f "$MEMORY_FILE" ]; then
    printf '%s' "$fallback"
    return
  fi

  local value
  value=$(jq -r --arg key "$key" '.[$key] // empty' "$MEMORY_FILE")
  if [ -z "$value" ] || [ "$value" = "null" ]; then
    printf '%s' "$fallback"
  else
    printf '%s' "$value"
  fi
}

save_state() {
  local last_update_id="$1"
  local last_send_comment_id="$2"
  local tmp_file
  tmp_file="$(mktemp)"

  jq -n \
    --argjson last_update_id "$last_update_id" \
    --arg last_send_comment_id "$last_send_comment_id" \
    '{
      last_update_id: $last_update_id,
      last_send_comment_id: $last_send_comment_id
    }' > "$tmp_file"

  mv "$tmp_file" "$MEMORY_FILE"
}

sanitize_text() {
  printf '%s' "$1" | sed -E 's/sk-[[:alnum:]_-]{20,}/[REDACTED_OPENAI_KEY]/g'
}

format_timestamp() {
  local timestamp="$1"
  date -r "$timestamp" '+%Y-%m-%d %H:%M:%S %Z'
}

download_telegram_file() {
  local file_id="$1"
  local metadata
  local file_path
  local tmp_dir
  local output_path

  metadata="$(curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getFile?file_id=${file_id}")"
  file_path="$(printf '%s' "$metadata" | jq -r '.result.file_path // empty')"
  if [ -z "$file_path" ]; then
    echo "Telegram getFile failed for file_id=$file_id" >&2
    exit 1
  fi

  tmp_dir="$(mktemp -d)"
  output_path="$tmp_dir/$(basename "$file_path")"
  curl -fsS "https://api.telegram.org/file/bot${TELEGRAM_BOT_TOKEN}/${file_path}" -o "$output_path"
  printf '%s' "$output_path"
}

transcribe_audio_file() {
  local audio_path="$1"
  local response
  local transcript
  local model="${OPENAI_TRANSCRIPTION_MODEL:-gpt-4o-mini-transcribe}"

  require_env OPENAI_API_KEY

  if [ -n "${OPENAI_TRANSCRIPTION_LANGUAGE:-}" ]; then
    response="$(curl -fsS -X POST "https://api.openai.com/v1/audio/transcriptions" \
      -H "Authorization: Bearer $OPENAI_API_KEY" \
      -F "file=@${audio_path}" \
      -F "model=${model}" \
      -F "language=${OPENAI_TRANSCRIPTION_LANGUAGE}" \
      -F "response_format=json")"
  else
    response="$(curl -fsS -X POST "https://api.openai.com/v1/audio/transcriptions" \
      -H "Authorization: Bearer $OPENAI_API_KEY" \
      -F "file=@${audio_path}" \
      -F "model=${model}" \
      -F "response_format=json")"
  fi

  transcript="$(printf '%s' "$response" | jq -r '.text // empty')"
  if [ -z "$transcript" ]; then
    echo "OpenAI transcription returned an empty transcript" >&2
    exit 1
  fi

  printf '%s' "$transcript"
}

transcribe_telegram_voice() {
  local file_id="$1"
  local audio_path
  local transcript

  audio_path="$(download_telegram_file "$file_id")"
  transcript="$(transcribe_audio_file "$audio_path")"
  rm -rf "$(dirname "$audio_path")"

  printf '%s' "$transcript"
}

ensure_checkout() {
  if [ "$CHECKED_OUT" -eq 1 ]; then
    return
  fi

  curl -fsS -X POST \
    -H "Authorization: Bearer $PAPERCLIP_API_KEY" \
    -H "X-Paperclip-Run-Id: $PAPERCLIP_RUN_ID" \
    -H "Content-Type: application/json" \
    "$PAPERCLIP_API_URL/api/issues/$ISSUE_ID/checkout" \
    -d "{\"agentId\":\"$PAPERCLIP_AGENT_ID\",\"expectedStatuses\":[\"todo\",\"backlog\",\"blocked\",\"in_progress\"]}" \
    > /dev/null

  CHECKED_OUT=1
}

post_issue_comment() {
  local body="$1"
  ensure_checkout

  curl -fsS -X POST \
    -H "Authorization: Bearer $PAPERCLIP_API_KEY" \
    -H "X-Paperclip-Run-Id: $PAPERCLIP_RUN_ID" \
    -H "Content-Type: application/json" \
    "$PAPERCLIP_API_URL/api/issues/$ISSUE_ID/comments" \
    -d "{\"body\":$(printf '%s' "$body" | jq -Rs .)}" \
    > /dev/null
}

fetch_issue_comments() {
  local url="$PAPERCLIP_API_URL/api/issues/$ISSUE_ID/comments"
  if [ -n "$1" ]; then
    url="$url?after=$1&order=asc"
  fi
  curl -fsS -H "Authorization: Bearer $PAPERCLIP_API_KEY" "$url"
}

send_to_telegram() {
  local text="$1"
  curl -fsS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=$TELEGRAM_CHAT_ID" \
    --data-urlencode "text=$text" \
    > /dev/null
}

require_env TELEGRAM_BOT_TOKEN
require_env PAPERCLIP_API_KEY
require_env PAPERCLIP_API_URL
require_env PAPERCLIP_RUN_ID
require_env PAPERCLIP_AGENT_ID

mkdir -p "$(dirname "$MEMORY_FILE")"

LAST_UPDATE_ID="$(load_state_value "last_update_id" "0")"
LAST_SEND_COMMENT_ID="$(load_state_value "last_send_comment_id" "")"
NEXT_OFFSET=$((LAST_UPDATE_ID + 1))

echo "Polling Telegram from offset $NEXT_OFFSET..."
TG_RESPONSE="$(curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates?offset=${NEXT_OFFSET}")"
TG_OK="$(printf '%s' "$TG_RESPONSE" | jq -r '.ok')"
if [ "$TG_OK" != "true" ]; then
  echo "Telegram getUpdates failed" >&2
  exit 1
fi

UPDATES="$(printf '%s' "$TG_RESPONSE" | jq -r '.result | length')"
HIGHEST_UPDATE_ID="$LAST_UPDATE_ID"

if [ "$UPDATES" -gt 0 ]; then
  echo "Found $UPDATES new Telegram messages"
  HIGHEST_UPDATE_ID="$(printf '%s' "$TG_RESPONSE" | jq -r '.result | max_by(.update_id) | .update_id')"

  while IFS= read -r UPDATE; do
    MESSAGE="$(printf '%s' "$UPDATE" | jq -c '.message // empty')"
    if [ -z "$MESSAGE" ] || [ "$MESSAGE" = "null" ]; then
      continue
    fi

    SENDER_ID="$(printf '%s' "$MESSAGE" | jq -r '.from.id')"
    SENDER_NAME="$(printf '%s' "$MESSAGE" | jq -r '(.from.first_name // "") + " " + (.from.last_name // "")' | sed 's/[[:space:]]*$//' | sed 's/^[[:space:]]*//')"
    TIMESTAMP="$(printf '%s' "$MESSAGE" | jq -r '.date')"
    FORMATTED_TIME="$(format_timestamp "$TIMESTAMP")"

    TEXT="$(printf '%s' "$MESSAGE" | jq -r '.text // empty')"
    VOICE_DURATION="$(printf '%s' "$MESSAGE" | jq -r '.voice.duration // empty')"
    VOICE_FILE_ID="$(printf '%s' "$MESSAGE" | jq -r '.voice.file_id // empty')"
    DOCUMENT_NAME="$(printf '%s' "$MESSAGE" | jq -r '.document.file_name // empty')"
    HAS_PHOTO="$(printf '%s' "$MESSAGE" | jq -r 'if .photo then "yes" else "" end')"
    HAS_VIDEO="$(printf '%s' "$MESSAGE" | jq -r 'if .video then "yes" else "" end')"

    if [ -n "$TEXT" ]; then
      SAFE_TEXT="$(sanitize_text "$TEXT")"
      COMMENT_BODY="**From: $SENDER_NAME** ($SENDER_ID) at $FORMATTED_TIME
\`\`\`
$SAFE_TEXT
\`\`\`"
    elif [ -n "$VOICE_DURATION" ]; then
      if [ -z "$VOICE_FILE_ID" ]; then
        echo "Voice message missing file_id" >&2
        exit 1
      fi

      TRANSCRIPT="$(transcribe_telegram_voice "$VOICE_FILE_ID")"
      SAFE_TRANSCRIPT="$(sanitize_text "$TRANSCRIPT")"
      COMMENT_BODY="**From: $SENDER_NAME** ($SENDER_ID) at $FORMATTED_TIME
[Voice message, ${VOICE_DURATION}s]
\`\`\`
$SAFE_TRANSCRIPT
\`\`\`"
    elif [ -n "$DOCUMENT_NAME" ]; then
      COMMENT_BODY="**From: $SENDER_NAME** ($SENDER_ID) at $FORMATTED_TIME
[Document: $DOCUMENT_NAME]"
    elif [ -n "$HAS_PHOTO" ]; then
      COMMENT_BODY="**From: $SENDER_NAME** ($SENDER_ID) at $FORMATTED_TIME
[Photo]"
    elif [ -n "$HAS_VIDEO" ]; then
      COMMENT_BODY="**From: $SENDER_NAME** ($SENDER_ID) at $FORMATTED_TIME
[Video]"
    else
      COMMENT_BODY="**From: $SENDER_NAME** ($SENDER_ID) at $FORMATTED_TIME
[Unsupported message type]"
    fi

    echo "Posting Telegram message from $SENDER_NAME to WIN-12..."
    post_issue_comment "$COMMENT_BODY"
  done < <(printf '%s' "$TG_RESPONSE" | jq -c '.result[]')
fi

echo "Checking WIN-12 for new SEND: messages..."
NEW_LAST_SEND_COMMENT_ID="$LAST_SEND_COMMENT_ID"

if [ -z "$LAST_SEND_COMMENT_ID" ]; then
  COMMENTS_RESPONSE="$(fetch_issue_comments "")"
  NEW_LAST_SEND_COMMENT_ID="$(printf '%s' "$COMMENTS_RESPONSE" | jq -r '.[0].id // ""')"
  echo "Initialized SEND cursor to current latest comment"
else
  COMMENTS_RESPONSE="$(fetch_issue_comments "$LAST_SEND_COMMENT_ID")"

  while IFS= read -r COMMENT; do
    COMMENT_ID="$(printf '%s' "$COMMENT" | jq -r '.id')"
    AUTHOR_AGENT_ID="$(printf '%s' "$COMMENT" | jq -r '.authorAgentId // ""')"
    BODY="$(printf '%s' "$COMMENT" | jq -r '.body // ""')"

    NEW_LAST_SEND_COMMENT_ID="$COMMENT_ID"

    if [ "$AUTHOR_AGENT_ID" != "$CEO_AGENT_ID" ]; then
      continue
    fi

    if ! printf '%s' "$BODY" | grep -Eq '^SEND:[[:space:]]*'; then
      continue
    fi

    MESSAGE_TEXT="$(printf '%s' "$BODY" | sed -E '1s/^SEND:[[:space:]]*//' | head -c 500)"
    if [ -z "$MESSAGE_TEXT" ]; then
      continue
    fi

    echo "Sending CEO message to Telegram"
    send_to_telegram "$MESSAGE_TEXT"
    post_issue_comment "Relayed CEO message to Telegram for chat \`$TELEGRAM_CHAT_ID\`."
  done < <(printf '%s' "$COMMENTS_RESPONSE" | jq -c '.[]')
fi

echo "Saving state (last_update_id=$HIGHEST_UPDATE_ID, last_send_comment_id=$NEW_LAST_SEND_COMMENT_ID)"
save_state "$HIGHEST_UPDATE_ID" "$NEW_LAST_SEND_COMMENT_ID"

echo "Polling cycle complete"
