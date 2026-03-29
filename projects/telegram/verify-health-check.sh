#!/bin/bash
# verify-health-check.sh — Safe local verification for Telegram health-check.sh.
# Exercises both the healthy and stale paths using temp files and dry-run alerting.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HEALTH_CHECK="$SCRIPT_DIR/health-check.sh"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

DATA_DIR="$TMP_DIR/data"
LOG_DIR="$TMP_DIR/logs"
ENV_FILE="$TMP_DIR/.env"

mkdir -p "$DATA_DIR" "$LOG_DIR"
printf 'TELEGRAM_BOT_TOKEN=dry-run-token\n' > "$ENV_FILE"

PASS=0
FAIL=0

pass() {
  printf "  [PASS] %s\n" "$1"
  PASS=$((PASS + 1))
}

fail() {
  printf "  [FAIL] %s\n" "$1"
  FAIL=$((FAIL + 1))
}

iso_utc_from_epoch() {
  python3 - "$1" <<'PY'
import datetime
import sys

epoch = int(sys.argv[1])
print(datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
PY
}

run_health_check() {
  TELEGRAM_HEALTH_DATA_DIR="$DATA_DIR" \
  TELEGRAM_HEALTH_ENV_FILE="$ENV_FILE" \
  TELEGRAM_HEALTH_LAUNCHD_LOG_DIR="$LOG_DIR" \
  TELEGRAM_ALERT_DRY_RUN=1 \
  "$HEALTH_CHECK"
}

assert_log_contains() {
  local pattern="$1"
  if rg -q "$pattern" "$DATA_DIR/health.log"; then
    pass "health.log contains: $pattern"
  else
    fail "health.log missing: $pattern"
  fi
}

printf "\nHealthy path\n"
rm -f "$DATA_DIR/health.log" "$DATA_DIR/health-state.json"
RECENT_TS="$(iso_utc_from_epoch "$(( $(date +%s) - 120 ))")"
jq -n --arg last_poll "$RECENT_TS" '{"last_poll": $last_poll}' > "$DATA_DIR/state.json"
run_health_check
assert_log_contains 'OK: poller healthy'
if [ ! -f "$DATA_DIR/health-state.json" ]; then
  pass "healthy run did not create alert state"
else
  LAST_ALERT="$(jq -r '.last_alert // 0' "$DATA_DIR/health-state.json")"
  if [ "$LAST_ALERT" = "0" ]; then
    pass "healthy run reset existing alert cooldown"
  else
    fail "healthy run left non-zero last_alert=$LAST_ALERT"
  fi
fi

printf "\nStale path\n"
rm -f "$DATA_DIR/health.log" "$DATA_DIR/health-state.json"
STALE_TS="$(iso_utc_from_epoch "$(( $(date +%s) - 900 ))")"
jq -n --arg last_poll "$STALE_TS" '{"last_poll": $last_poll}' > "$DATA_DIR/state.json"
run_health_check
assert_log_contains 'ALERT\(dry-run\): poller stale'
if [ -f "$DATA_DIR/health-state.json" ]; then
  LAST_STALE_POLL="$(jq -r '.last_stale_poll // empty' "$DATA_DIR/health-state.json")"
  if [ "$LAST_STALE_POLL" = "$STALE_TS" ]; then
    pass "stale run recorded alert state"
  else
    fail "stale run recorded unexpected last_stale_poll=$LAST_STALE_POLL"
  fi
else
  fail "stale run did not create health-state.json"
fi

printf "\nSummary: %d passed, %d failed\n" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
