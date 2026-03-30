#!/bin/bash
# health-check.sh — WhatsApp poller health monitor.
# Runs every 2 minutes via launchd. Sends a Telegram alert if the poller has not
# successfully polled in 5+ minutes.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="${WHATSAPP_HEALTH_DATA_DIR:-$SCRIPT_DIR/data}"
STATE_FILE="${WHATSAPP_HEALTH_STATE_FILE:-$DATA_DIR/state.json}"
HEALTH_STATE_FILE="${WHATSAPP_HEALTH_ALERT_STATE_FILE:-$DATA_DIR/health-state.json}"
LOG_FILE="${WHATSAPP_HEALTH_LOG_FILE:-$DATA_DIR/health.log}"
ENV_FILE="${WHATSAPP_HEALTH_ENV_FILE:-$SCRIPT_DIR/../telegram/.env}"

STALE_THRESHOLD_SEC=300
ALERT_COOLDOWN_SEC=600
OWNER_CHAT_ID="${TELEGRAM_CHAT_ID:-528866003}"
LOG_MAX_LINES=500
LAUNCHD_LOG_DIR="${WHATSAPP_HEALTH_LAUNCHD_LOG_DIR:-$HOME/.paperclip/logs}"
DRY_RUN="${WHATSAPP_ALERT_DRY_RUN:-0}"

if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

mkdir -p "$DATA_DIR"

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
rotate_log "$LAUNCHD_LOG_DIR/whatsapp-health-stdout.log"
rotate_log "$LAUNCHD_LOG_DIR/whatsapp-health-stderr.log"

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
  echo "$(date -Iseconds) ERROR: TELEGRAM_BOT_TOKEN not set" >> "$LOG_FILE"
  exit 1
fi

if [ ! -f "$STATE_FILE" ]; then
  echo "$(date -Iseconds) WARN: state.json not found — poller may never have run" >> "$LOG_FILE"
  exit 0
fi

LAST_POLL="$(jq -r '.last_poll // empty' "$STATE_FILE" 2>/dev/null || true)"
if [ -z "$LAST_POLL" ]; then
  echo "$(date -Iseconds) WARN: last_poll missing from state.json" >> "$LOG_FILE"
  exit 0
fi

LAST_POLL_EPOCH="$(date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "$LAST_POLL" "+%s" 2>/dev/null)" || {
  echo "$(date -Iseconds) ERROR: Could not parse last_poll timestamp: $LAST_POLL" >> "$LOG_FILE"
  exit 1
}

NOW="$(date +%s)"
ELAPSED=$(( NOW - LAST_POLL_EPOCH ))

if [ "$ELAPSED" -lt "$STALE_THRESHOLD_SEC" ]; then
  echo "$(date -Iseconds) OK: poller healthy (last poll ${ELAPSED}s ago)" >> "$LOG_FILE"
  if [ -f "$HEALTH_STATE_FILE" ]; then
    tmp="$(mktemp)"
    jq '.last_alert = 0' "$HEALTH_STATE_FILE" > "$tmp" && mv "$tmp" "$HEALTH_STATE_FILE" || true
  fi
  exit 0
fi

LAST_ALERT=0
if [ -f "$HEALTH_STATE_FILE" ]; then
  LAST_ALERT="$(jq -r '.last_alert // 0' "$HEALTH_STATE_FILE" 2>/dev/null || echo 0)"
fi

TIME_SINCE_ALERT=$(( NOW - LAST_ALERT ))
if [ "$TIME_SINCE_ALERT" -lt "$ALERT_COOLDOWN_SEC" ]; then
  echo "$(date -Iseconds) WARN: poller stale (${ELAPSED}s) — cooldown active, last alert ${TIME_SINCE_ALERT}s ago" >> "$LOG_FILE"
  exit 0
fi

ELAPSED_MIN=$(( ELAPSED / 60 ))
ALERT_MSG="⚠️ WhatsApp poller is stale — last successful poll was ${ELAPSED_MIN}m ago.

Check launchd status:
  launchctl list com.paperclip.whatsapp-poll

Recent poller log:
  tail ~/.paperclip/logs/whatsapp-poll-stdout.log"

if [ "$DRY_RUN" = "1" ]; then
  echo "$(date -Iseconds) ALERT(dry-run): poller stale (${ELAPSED}s / ${ELAPSED_MIN}m) — Telegram alert suppressed" >> "$LOG_FILE"
else
  curl -fsS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    -d "chat_id=${OWNER_CHAT_ID}" \
    --data-urlencode "text=${ALERT_MSG}" \
    -o /dev/null || {
      echo "$(date -Iseconds) ERROR: Failed to send Telegram alert" >> "$LOG_FILE"
      exit 1
    }
fi

jq -n --argjson ts "$NOW" --arg last_poll "$LAST_POLL" \
  '{"last_alert": $ts, "last_stale_poll": $last_poll}' > "$HEALTH_STATE_FILE"

if [ "$DRY_RUN" != "1" ]; then
  echo "$(date -Iseconds) ALERT: poller stale (${ELAPSED}s / ${ELAPSED_MIN}m) — Telegram alert sent" >> "$LOG_FILE"
fi
