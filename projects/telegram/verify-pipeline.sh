#!/bin/bash
# verify-pipeline.sh — End-to-end verification of the Telegram → Chief wake pipeline.
# Run on-demand to confirm the pipeline works after changes.
#
# Usage:
#   ./verify-pipeline.sh              # checks only (safe, no side effects)
#   ./verify-pipeline.sh --test-wake  # also fires a real /wake to confirm end-to-end
#
# Exit code: 0 = all checks passed, 1 = one or more failures.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
DATA_DIR="$SCRIPT_DIR/data"
STATE_FILE="$DATA_DIR/state.json"

LAUNCHD_LABEL="com.paperclip.telegram-poll"
LAUNCHD_PATH="$HOME/Library/LaunchAgents/com.paperclip.telegram-poll.plist"
LAUNCHD_PATH_SETTING="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
PAPERCLIP_API_BASE="${PAPERCLIP_API_BASE:-http://localhost:3100}"
CEO_AGENT_ID="e2b797d0-8f0c-4bcf-adf9-99fd095ea14b"
CEO_ALERT_ISSUE_ID="0d1502be-da96-4db1-bfe4-de78c19e473a"
OWNER_CHAT_ID="528866003"

TEST_WAKE=0
if [[ "${1:-}" == "--test-wake" ]]; then
  TEST_WAKE=1
fi

PASS=0
FAIL=0

pass() { printf "  \033[32m✓\033[0m %s\n" "$1"; PASS=$((PASS+1)); }
fail() { printf "  \033[31m✗\033[0m %s\n" "$1"; FAIL=$((FAIL+1)); }
warn() { printf "  \033[33m~\033[0m %s\n" "$1"; }
section() { printf "\n\033[1m=== %s ===\033[0m\n" "$1"; }

# ------------------------------------------------------------------
# 1. launchd
# ------------------------------------------------------------------
section "launchd"

if [ -f "$LAUNCHD_PATH" ]; then
  pass "plist file exists: $LAUNCHD_PATH"
else
  fail "plist NOT found: $LAUNCHD_PATH"
fi

LAUNCHD_ROW="$(launchctl list 2>/dev/null | grep "$LAUNCHD_LABEL" || true)"
if [ -n "$LAUNCHD_ROW" ]; then
  LAST_EXIT="$(echo "$LAUNCHD_ROW" | awk '{print $1}')"
  PID="$(echo "$LAUNCHD_ROW" | awk '{print $2}')"
  if [ "$LAST_EXIT" = "0" ] || [ "$LAST_EXIT" = "-" ]; then
    pass "launchd job loaded — last exit: ${LAST_EXIT} (PID slot: ${PID})"
  else
    fail "launchd job loaded but last exit code = $LAST_EXIT (non-zero means last run failed)"
  fi
else
  fail "launchd job NOT loaded: $LAUNCHD_LABEL"
  warn "To load: launchctl load $LAUNCHD_PATH"
fi

# ------------------------------------------------------------------
# 2. PATH / dependencies
# ------------------------------------------------------------------
section "Dependencies (launchd PATH)"

# Test tools in the same PATH launchd injects
for tool in jq curl; do
  FOUND="$(PATH="$LAUNCHD_PATH_SETTING" command -v "$tool" 2>/dev/null || true)"
  if [ -n "$FOUND" ]; then
    pass "$tool found at $FOUND"
  else
    fail "$tool NOT found in launchd PATH ($LAUNCHD_PATH_SETTING)"
  fi
done

# ------------------------------------------------------------------
# 3. .env required vars
# ------------------------------------------------------------------
section ".env"

if [ -f "$ENV_FILE" ]; then
  pass ".env file exists"
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
else
  fail ".env file NOT found: $ENV_FILE"
fi

for var in TELEGRAM_BOT_TOKEN PAPERCLIP_CEO_API_KEY; do
  val="${!var:-}"
  if [ -n "$val" ]; then
    masked="${val:0:6}…${val: -4}"
    pass "$var set ($masked)"
  else
    fail "$var NOT set in .env"
  fi
done

# ------------------------------------------------------------------
# 4. Telegram API
# ------------------------------------------------------------------
section "Telegram API"

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
  RESP="$(curl -fsS --max-time 10 \
    "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" 2>/dev/null)" || RESP=""
  OK="$(printf '%s' "$RESP" | jq -r '.ok' 2>/dev/null || echo 'error')"
  if [ "$OK" = "true" ]; then
    BOT="$(printf '%s' "$RESP" | jq -r '.result.username')"
    pass "Telegram API reachable (bot: @$BOT)"
  else
    fail "Telegram API unreachable or returned ok=false"
  fi
else
  fail "TELEGRAM_BOT_TOKEN not set — skipping Telegram check"
fi

# ------------------------------------------------------------------
# 5. Paperclip API
# ------------------------------------------------------------------
section "Paperclip API"

if [ -n "${PAPERCLIP_CEO_API_KEY:-}" ]; then
  HTTP="$(curl -fsS -o /dev/null -w "%{http_code}" --max-time 10 \
    -H "Authorization: Bearer ${PAPERCLIP_CEO_API_KEY}" \
    "${PAPERCLIP_API_BASE}/api/agents/me" 2>/dev/null)" || HTTP="000"
  if [ "$HTTP" = "200" ]; then
    pass "Paperclip API reachable at $PAPERCLIP_API_BASE (HTTP $HTTP)"
  else
    fail "Paperclip API returned HTTP $HTTP at $PAPERCLIP_API_BASE"
  fi
else
  fail "PAPERCLIP_CEO_API_KEY not set — skipping Paperclip check"
fi

# ------------------------------------------------------------------
# 6. CEO alert issue accessible (WIN-28)
# ------------------------------------------------------------------
section "CEO Alert Issue"

if [ -n "${PAPERCLIP_CEO_API_KEY:-}" ]; then
  HTTP="$(curl -fsS -o /dev/null -w "%{http_code}" --max-time 10 \
    -H "Authorization: Bearer ${PAPERCLIP_CEO_API_KEY}" \
    "${PAPERCLIP_API_BASE}/api/issues/${CEO_ALERT_ISSUE_ID}" 2>/dev/null)" || HTTP="000"
  if [ "$HTTP" = "200" ]; then
    pass "CEO alert issue accessible (WIN-28, id=$CEO_ALERT_ISSUE_ID)"
  else
    fail "CEO alert issue returned HTTP $HTTP — wake target unreachable"
  fi
else
  fail "PAPERCLIP_CEO_API_KEY not set — skipping CEO issue check"
fi

# ------------------------------------------------------------------
# 7. State file / data directory
# ------------------------------------------------------------------
section "State"

if [ -d "$DATA_DIR" ]; then
  pass "data/ directory exists"
else
  fail "data/ directory missing — run cron-poll.sh at least once"
fi

if [ -f "$STATE_FILE" ]; then
  LAST_POLL="$(jq -r '.last_poll // "never"' "$STATE_FILE" 2>/dev/null || echo unknown)"
  LAST_OFFSET="$(jq -r '.last_update_id // 0' "$STATE_FILE" 2>/dev/null || echo unknown)"
  LAST_WAKE="$(jq -r '.last_auto_wake // 0' "$STATE_FILE" 2>/dev/null || echo 0)"
  NOW="$(date +%s)"
  WAKE_AGO=$(( NOW - LAST_WAKE ))
  WAKE_HUMAN="${WAKE_AGO}s ago"
  pass "state.json exists (last_poll=$LAST_POLL, offset=$LAST_OFFSET, last_auto_wake=${WAKE_HUMAN})"
else
  fail "state.json NOT found — poller may never have run"
fi

if [ -f "$DATA_DIR/inbox.jsonl" ]; then
  LINE_COUNT="$(wc -l < "$DATA_DIR/inbox.jsonl" | tr -d ' ')"
  pass "inbox.jsonl exists ($LINE_COUNT entries)"
else
  warn "inbox.jsonl not found (no messages received yet)"
fi

# ------------------------------------------------------------------
# 8. Optional live wake test
# ------------------------------------------------------------------
section "Auto-wake trigger"

if [ "$TEST_WAKE" -eq 1 ]; then
  echo "  Firing live wake test — will post @Chief comment on WIN-28..."
  BODY='{"body":"@Chief verify-pipeline.sh: live wake test"}'
  HTTP="$(curl -fsS -o /dev/null -w "%{http_code}" --max-time 10 \
    -H "Authorization: Bearer ${PAPERCLIP_CEO_API_KEY}" \
    -H "Content-Type: application/json" \
    -d "$BODY" \
    "${PAPERCLIP_API_BASE}/api/issues/${CEO_ALERT_ISSUE_ID}/comments" 2>/dev/null)" || HTTP="000"
  if [ "$HTTP" = "200" ] || [ "$HTTP" = "201" ]; then
    pass "Wake comment posted successfully (HTTP $HTTP) — check CEO heartbeat log to confirm wake"
  else
    fail "Failed to post wake comment (HTTP $HTTP)"
  fi
else
  warn "Skipped — re-run with --test-wake to fire a real wake and verify end-to-end"
fi

# ------------------------------------------------------------------
# Summary
# ------------------------------------------------------------------
printf "\n\033[1m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m\n"
if [ "$FAIL" -eq 0 ]; then
  printf "  \033[32mAll %d checks passed.\033[0m\n" "$PASS"
else
  printf "  \033[31m%d failed\033[0m, %d passed\n" "$FAIL" "$PASS"
fi
printf "\033[1m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m\n\n"

[ "$FAIL" -eq 0 ]
