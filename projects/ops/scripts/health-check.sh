#!/usr/bin/env bash
# Health check for all Paperclip LaunchAgents, production server, and data freshness.
# Outputs JSON summary to stdout and appends to health.log.
# Alerts Chief via Telegram on any failure.
set -uo pipefail

BASE_DIR="/Users/fabiodomingues/Desktop/Projects/paperclip"
SEND_SH="$BASE_DIR/projects/telegram/send.sh"
LOG_FILE="$BASE_DIR/projects/ops/scripts/health.log"
STALE_THRESHOLD_MIN=60
TELEGRAM_POLL_STALE_THRESHOLD_MIN="${TELEGRAM_POLL_STALE_THRESHOLD_MIN:-10}"
WHATSAPP_POLL_STALE_THRESHOLD_MIN="${WHATSAPP_POLL_STALE_THRESHOLD_MIN:-5}"
WHATSAPP_SYNC_STALE_THRESHOLD_MIN="${WHATSAPP_SYNC_STALE_THRESHOLD_MIN:-90}"
SERVER="root@64.226.74.167"
VIEWER_LABEL="com.paperclip.whatsapp-viewer"
VIEWER_HEALTH_URL="${WHATSAPP_VIEWER_HEALTH_URL:-http://127.0.0.1:5050/healthz}"
VIEWER_HEALTH_TIMEOUT_SEC="${WHATSAPP_VIEWER_HEALTH_TIMEOUT_SEC:-3}"

TIMESTAMP=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
HEALTHY=true
ALERTS=""

add_alert() {
    if [ -n "$ALERTS" ]; then ALERTS="$ALERTS|$1"; else ALERTS="$1"; fi
}

json_escape() {
    printf '%s' "$1" | python3 -c 'import json, sys; print(json.dumps(sys.stdin.read()))'
}

# launchctl exit/signals are historical for KeepAlive jobs. Use the viewer's
# HTTP probe as the live signal, and treat launchctl history as responder context.
VIEWER_PRINT="$(launchctl print "gui/$(id -u)/$VIEWER_LABEL" 2>/dev/null || true)"
VIEWER_STATE="$(printf '%s\n' "$VIEWER_PRINT" | sed -n 's/^[[:space:]]*state = //p' | head -n1 | tr -d '[:space:]')"
VIEWER_PRINT_PID_RAW="$(printf '%s\n' "$VIEWER_PRINT" | sed -n 's/^[[:space:]]*pid = //p' | head -n1 | tr -d '[:space:]')"
VIEWER_LAST_SIGNAL="$(printf '%s\n' "$VIEWER_PRINT" | sed -n 's/^[[:space:]]*last terminating signal = //p' | head -n1)"
VIEWER_HTTP_OK=false
VIEWER_HTTP_CODE=null
VIEWER_HTTP_ERROR=""
VIEWER_CURL_EXIT=0
VIEWER_CURL_ERR="$(mktemp)"
VIEWER_HTTP_CODE_RAW="$(curl -sS -o /dev/null -w '%{http_code}' \
    --connect-timeout 1 \
    --max-time "$VIEWER_HEALTH_TIMEOUT_SEC" \
    "$VIEWER_HEALTH_URL" 2>"$VIEWER_CURL_ERR")" || VIEWER_CURL_EXIT=$?
if [ "$VIEWER_CURL_EXIT" -eq 0 ] && [ "$VIEWER_HTTP_CODE_RAW" = "200" ]; then
    VIEWER_HTTP_OK=true
fi
if [ -n "$VIEWER_HTTP_CODE_RAW" ] && [ "$VIEWER_HTTP_CODE_RAW" != "000" ]; then
    VIEWER_HTTP_CODE="$VIEWER_HTTP_CODE_RAW"
fi
if [ -s "$VIEWER_CURL_ERR" ]; then
    VIEWER_HTTP_ERROR="$(tr '\n' ' ' < "$VIEWER_CURL_ERR" | sed 's/[[:space:]]\+/ /g; s/^ //; s/ $//')"
fi
rm -f "$VIEWER_CURL_ERR"

VIEWER_PID=null
VIEWER_LIVE_PID=false
if [ -n "$VIEWER_PRINT_PID_RAW" ] && [ "$VIEWER_PRINT_PID_RAW" != "0" ]; then
    VIEWER_PID="$VIEWER_PRINT_PID_RAW"
    VIEWER_LIVE_PID=true
fi

VIEWER_OK=$VIEWER_HTTP_OK
if ! $VIEWER_HTTP_OK; then
    HEALTHY=false
    add_alert "Viewer: health probe failed ($VIEWER_HEALTH_URL code=${VIEWER_HTTP_CODE_RAW:-000})"
fi

VIEWER_JSON="{\"health_url\":$(json_escape "$VIEWER_HEALTH_URL"),\"http_ok\":$VIEWER_HTTP_OK,\"http_code\":$VIEWER_HTTP_CODE,\"launchctl_state\":$(json_escape "$VIEWER_STATE"),\"pid\":$VIEWER_PID,\"live_pid\":$VIEWER_LIVE_PID,\"last_terminating_signal\":$(json_escape "$VIEWER_LAST_SIGNAL"),\"ok\":$VIEWER_OK"
if [ -n "$VIEWER_HTTP_ERROR" ]; then
    VIEWER_JSON+=",\"http_error\":$(json_escape "$VIEWER_HTTP_ERROR")"
fi
VIEWER_JSON+="}"

# --- A. LaunchAgent Health ---
LA_JSON="{"
first=true
while IFS=$'\t' read -r pid exit_code label; do
    case "$label" in com.paperclip.*) ;; *) continue ;; esac
    ok=true
    exit_val="$exit_code"
    pid_val="$pid"
    [ "$exit_code" = "-" ] && exit_val="null"
    [ "$pid" = "-" ] && pid_val="null"
    if [ "$exit_code" != "0" ] && [ "$exit_code" != "-" ]; then
        # KeepAlive services may show a stale exit code but still be running
        if [ "$label" = "$VIEWER_LABEL" ] && { $VIEWER_LIVE_PID || $VIEWER_HTTP_OK; }; then
            ok=true
        elif [ "$pid" != "-" ] && [ -n "$pid" ]; then
            ok=true  # Running with PID — previous crash was recovered
        else
            ok=false
            if [ "$label" != "$VIEWER_LABEL" ]; then
                HEALTHY=false
                add_alert "LaunchAgent $label exit=$exit_code"
            fi
        fi
    fi
    $first || LA_JSON+=","
    LA_JSON+="\"$label\":{\"exit\":$exit_val,\"pid\":$pid_val,\"ok\":$ok}"
    first=false
done < <(launchctl list 2>/dev/null | grep com.paperclip)
LA_JSON+="}"

# --- B. Production Server Health ---
# SSH probe: single connection using ControlMaster so concurrent health-check
# runs share the same TCP session (prevents UFW rate-limit false positives).
# BatchMode=yes avoids hanging on a password prompt if key auth breaks.
SSH_CONTROL_PATH="/tmp/paperclip-ssh-health-%r@%h-%p"
SSH_OPTS=(-o ConnectTimeout=10 -o StrictHostKeyChecking=no -o BatchMode=yes \
          -o ControlMaster=auto -o "ControlPath=$SSH_CONTROL_PATH" \
          -o ControlPersist=120)
SSH_OK=false
CONTAINER_STATUS="unknown"
SSH_EXIT=0
SSH_OUT=$(ssh "${SSH_OPTS[@]}" "$SERVER" \
    "printf '__CONNECTED__\n'; docker ps --format '{{.Names}} {{.Status}}' 2>/dev/null" \
    2>/dev/null) || SSH_EXIT=$?

if [ "$SSH_EXIT" -eq 0 ] && echo "$SSH_OUT" | grep -q "__CONNECTED__"; then
    SSH_OK=true
    if echo "$SSH_OUT" | grep -q "remodelai-app"; then
        CONTAINER_STATUS="running"
    else
        CONTAINER_STATUS="not_running"
        HEALTHY=false
        add_alert "Server: remodelai-app container not running"
    fi
else
    SSH_OK=false
    CONTAINER_STATUS="ssh_failed"
    HEALTHY=false
    add_alert "Server: SSH connection failed"
fi

SERVER_JSON="{\"ssh\":$SSH_OK,\"containers\":{\"remodelai-app\":\"$CONTAINER_STATUS\"}}"

# --- C. Data Freshness ---
FRESH_JSON="{"
NOW_EPOCH=$(date +%s)
fresh_first=true

check_freshness() {
    local name="$1"
    local filepath="$2"
    local threshold_min="${3:-$STALE_THRESHOLD_MIN}"
    local source_mode="${4:-mtime}"
    $fresh_first || FRESH_JSON+=","
    fresh_first=false
    if [ ! -f "$filepath" ]; then
        FRESH_JSON+="\"$name\":{\"age_min\":null,\"ok\":false,\"error\":\"file_not_found\"}"
        HEALTHY=false
        add_alert "Data: $name file not found"
        return
    fi
    local sample=""
    local sample_epoch=0
    if [ "$source_mode" = "json_last_poll" ] || [ "$source_mode" = "json_last_run" ]; then
        if [ "$source_mode" = "json_last_run" ]; then
            # Prefer last_run (poll execution time); fall back to last_poll for
            # backward compat with state.json written before last_run was introduced.
            sample="$(jq -r '.last_run // .last_poll // empty' "$filepath" 2>/dev/null || true)"
        else
            sample="$(jq -r '.last_poll // empty' "$filepath" 2>/dev/null || true)"
        fi
        if [ -z "$sample" ]; then
            FRESH_JSON+="\"$name\":{\"age_min\":null,\"ok\":false,\"error\":\"missing_last_poll\"}"
            HEALTHY=false
            add_alert "Data: $name missing last_poll"
            return
        fi
        sample_epoch="$(python3 - "$sample" <<'PY'
import datetime
import sys

value = sys.argv[1]
try:
    dt = datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
except ValueError:
    print(0)
    raise SystemExit(0)

print(int(dt.replace(tzinfo=datetime.timezone.utc).timestamp()))
PY
)"
        if [ "$sample_epoch" -le 0 ]; then
            FRESH_JSON+="\"$name\":{\"age_min\":null,\"ok\":false,\"error\":\"invalid_last_poll\"}"
            HEALTHY=false
            add_alert "Data: $name invalid last_poll"
            return
        fi
    else
        sample_epoch="$(stat -f %m "$filepath" 2>/dev/null || echo 0)"
    fi
    local age_sec=$(( NOW_EPOCH - sample_epoch ))
    local age_min=$(( age_sec / 60 ))
    local ok=true
    if [ "$age_min" -gt "$threshold_min" ]; then
        ok=false
        HEALTHY=false
        add_alert "Data: $name stale (${age_min}m)"
    fi
    FRESH_JSON+="\"$name\":{\"age_min\":$age_min,\"ok\":$ok,\"threshold_min\":$threshold_min"
    if [ "$source_mode" = "json_last_poll" ] || [ "$source_mode" = "json_last_run" ]; then
        FRESH_JSON+=",\"last_poll\":$(json_escape "$sample")"
    fi
    FRESH_JSON+="}"
}

# telegram_poll: uses last_poll (Telegram poller advances it on every cycle).
# whatsapp_poll: uses last_run (poll execution time) with last_poll fallback — last_poll
#   only advances when a new inbound message arrives, so it is not a reliable liveness signal.
check_freshness "telegram_poll" "$BASE_DIR/projects/telegram/data/state.json" "$TELEGRAM_POLL_STALE_THRESHOLD_MIN" "json_last_poll"
check_freshness "whatsapp_poll" "$BASE_DIR/projects/whatsapp/data/state.json" "$WHATSAPP_POLL_STALE_THRESHOLD_MIN" "json_last_run"
check_freshness "whatsapp_sync" "$BASE_DIR/projects/whatsapp/data/sync.log" "$WHATSAPP_SYNC_STALE_THRESHOLD_MIN" "mtime"
FRESH_JSON+="}"

# Build alerts JSON array
ALERTS_JSON="["
if [ -n "$ALERTS" ]; then
    afirst=true
    IFS='|' read -ra ALERT_ARR <<< "$ALERTS"
    for alert in "${ALERT_ARR[@]}"; do
        $afirst || ALERTS_JSON+=","
        ALERTS_JSON+="\"$(echo "$alert" | sed 's/"/\\"/g')\""
        afirst=false
    done
fi
ALERTS_JSON+="]"

# --- Build Final JSON ---
RESULT="{\"timestamp\":\"$TIMESTAMP\",\"healthy\":$HEALTHY,\"viewer\":$VIEWER_JSON,\"launchagents\":$LA_JSON,\"server\":$SERVER_JSON,\"data_freshness\":$FRESH_JSON,\"alerts\":$ALERTS_JSON}"

# Pretty print to stdout
echo "$RESULT" | python3 -m json.tool 2>/dev/null || echo "$RESULT"

# Append to log
echo "[$TIMESTAMP] $RESULT" >> "$LOG_FILE"

# --- Alert on failure ---
# NOTE: Do NOT send Telegram messages from HealthBot. Only Chief sends to Fábio.
# Alerts are logged to health.log and stdout for the SRE agent to pick up via Paperclip.

exit 0
