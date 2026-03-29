#!/bin/bash
# verify-pipeline.sh — Verification for the Telegram → Chief wake pipeline.
# Run on-demand to confirm the pipeline works after changes.
#
# Usage:
#   ./verify-pipeline.sh              # checks only (safe, no side effects)
#   ./verify-pipeline.sh --test-wake  # also fires a real direct heartbeat wake
#
# Exit code: 0 = all checks passed, 1 = one or more failures.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
DATA_DIR="$SCRIPT_DIR/data"
STATE_FILE="$DATA_DIR/state.json"

LAUNCHD_LABEL="com.paperclip.telegram-poll"
LAUNCHD_PATH="$HOME/Library/LaunchAgents/com.paperclip.telegram-poll.plist"
LAUNCHD_PATH_SETTING="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
PAPERCLIP_API_BASE="${PAPERCLIP_API_BASE:-http://localhost:3100}"
CEO_AGENT_ID="e2b797d0-8f0c-4bcf-adf9-99fd095ea14b"
CEO_ALERT_ISSUE_ID="0d1502be-da96-4db1-bfe4-de78c19e473a"
OWNER_CHAT_ID="528866003"
POLLER_SCRIPT="$SCRIPT_DIR/cron-poll.sh"

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

prepend_path_dir() {
  local dir="$1"

  [ -n "$dir" ] || return 0
  [ -d "$dir" ] || return 0

  case ":$PATH:" in
    *":$dir:"*) ;;
    *) PATH="$dir:$PATH" ;;
  esac
}

resolve_default_nvm_bin() {
  local nvm_dir="${NVM_DIR:-$HOME/.nvm}"
  local default_alias
  local version
  local bin_dir

  [ -f "$nvm_dir/alias/default" ] || return 1

  default_alias="$(tr -d '[:space:]' < "$nvm_dir/alias/default")"
  [ -n "$default_alias" ] || return 1

  version="${default_alias#v}"
  bin_dir="$nvm_dir/versions/node/v${version}/bin"

  [ -x "$bin_dir/node" ] || return 1
  printf '%s\n' "$bin_dir"
}

ensure_launchd_node_path() {
  local nvm_bin

  nvm_bin="$(resolve_default_nvm_bin || true)"
  if [ -n "$nvm_bin" ]; then
    prepend_path_dir "$nvm_bin"
  fi

  export PATH
}

resolve_direct_wake_cli() {
  ensure_launchd_node_path

  if command -v paperclipai >/dev/null 2>&1; then
    printf 'paperclipai\t%s\n' "$(command -v paperclipai)"
    return 0
  fi

  if command -v npx >/dev/null 2>&1; then
    printf 'npx\t%s\n' "$(command -v npx)"
    return 0
  fi

  return 1
}

run_direct_wake_help() {
  if [ "$DIRECT_WAKE_CLI_KIND" = "paperclipai" ]; then
    paperclipai heartbeat run --help 2>/dev/null
  else
    npx --yes paperclipai heartbeat run --help 2>/dev/null
  fi
}

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

KEY_AGENT_ID=""
KEY_AGENT_NAME=""
KEY_AGENT_ROLE=""

if [ -n "${PAPERCLIP_CEO_API_KEY:-}" ]; then
  HTTP="$(curl -fsS -o /dev/null -w "%{http_code}" --max-time 10 \
    -H "Authorization: Bearer ${PAPERCLIP_CEO_API_KEY}" \
    "${PAPERCLIP_API_BASE}/api/agents/me" 2>/dev/null)" || HTTP="000"
  if [ "$HTTP" = "200" ]; then
    pass "Paperclip API reachable at $PAPERCLIP_API_BASE (HTTP $HTTP)"
    KEY_AGENT_INFO="$(curl -fsS --max-time 10 \
      -H "Authorization: Bearer ${PAPERCLIP_CEO_API_KEY}" \
      "${PAPERCLIP_API_BASE}/api/agents/me" 2>/dev/null || true)"
    KEY_AGENT_ID="$(printf '%s' "$KEY_AGENT_INFO" | jq -r '.id // empty' 2>/dev/null || true)"
    KEY_AGENT_NAME="$(printf '%s' "$KEY_AGENT_INFO" | jq -r '.name // empty' 2>/dev/null || true)"
    KEY_AGENT_ROLE="$(printf '%s' "$KEY_AGENT_INFO" | jq -r '.role // empty' 2>/dev/null || true)"
    if [ "$KEY_AGENT_ID" = "$CEO_AGENT_ID" ]; then
      pass "PAPERCLIP_CEO_API_KEY authenticates as Chief (${KEY_AGENT_ROLE:-unknown role})"
    else
      fail "PAPERCLIP_CEO_API_KEY authenticates as ${KEY_AGENT_NAME:-unknown} (${KEY_AGENT_ROLE:-unknown role}, ${KEY_AGENT_ID:-unknown id}), not Chief ($CEO_AGENT_ID)"
    fi
  else
    fail "Paperclip API returned HTTP $HTTP at $PAPERCLIP_API_BASE"
  fi
else
  fail "PAPERCLIP_CEO_API_KEY not set — skipping Paperclip check"
fi

# ------------------------------------------------------------------
# 6. Direct heartbeat path
# ------------------------------------------------------------------
section "Direct heartbeat wake"

ORIGINAL_PATH="$PATH"
PATH="$LAUNCHD_PATH_SETTING"
DIRECT_WAKE_INFO="$(resolve_direct_wake_cli 2>/dev/null || true)"
PATH="$ORIGINAL_PATH"

DIRECT_WAKE_CLI_KIND="${DIRECT_WAKE_INFO%%	*}"
if [ "$DIRECT_WAKE_CLI_KIND" != "$DIRECT_WAKE_INFO" ]; then
  DIRECT_WAKE_CLI_PATH="${DIRECT_WAKE_INFO#*	}"
else
  DIRECT_WAKE_CLI_KIND=""
  DIRECT_WAKE_CLI_PATH=""
fi

if [ "$DIRECT_WAKE_CLI_KIND" = "paperclipai" ]; then
  pass "launchd-equivalent PATH resolves paperclipai at $DIRECT_WAKE_CLI_PATH"
elif [ "$DIRECT_WAKE_CLI_KIND" = "npx" ]; then
  pass "launchd-equivalent PATH resolves npx at $DIRECT_WAKE_CLI_PATH for 'npx --yes paperclipai'"
else
  fail "launchd-equivalent PATH could not resolve paperclipai or npx"
fi

if [ -n "$DIRECT_WAKE_CLI_KIND" ]; then
  PATH="$LAUNCHD_PATH_SETTING"
  ensure_launchd_node_path
  HELP_OUTPUT="$(run_direct_wake_help || true)"
  PATH="$ORIGINAL_PATH"
else
  HELP_OUTPUT=""
fi

if printf '%s' "$HELP_OUTPUT" | grep -q -- '--agent-id' \
  && printf '%s' "$HELP_OUTPUT" | grep -q -- '--api-base' \
  && printf '%s' "$HELP_OUTPUT" | grep -q -- '--api-key' \
  && printf '%s' "$HELP_OUTPUT" | grep -q -- '--source' \
  && printf '%s' "$HELP_OUTPUT" | grep -q -- '--trigger'; then
  if [ "$DIRECT_WAKE_CLI_KIND" = "npx" ]; then
    pass "launchd-equivalent 'npx --yes paperclipai heartbeat run' exposes the expected direct-wake flags"
  else
    pass "launchd-equivalent 'paperclipai heartbeat run' exposes the expected direct-wake flags"
  fi
else
  fail "launchd-equivalent direct-wake command help is missing expected direct-wake flags"
fi

if grep -q 'paperclipai heartbeat run' "$POLLER_SCRIPT"; then
  pass "cron-poll.sh is wired to the direct heartbeat CLI"
else
  fail "cron-poll.sh does not reference paperclipai heartbeat run"
fi

# ------------------------------------------------------------------
# 7. Fallback route
# ------------------------------------------------------------------
section "Fallback wake path"

if [ -n "${PAPERCLIP_CEO_API_KEY:-}" ]; then
  HTTP="$(curl -fsS -o /dev/null -w "%{http_code}" --max-time 10 \
    -H "Authorization: Bearer ${PAPERCLIP_CEO_API_KEY}" \
    "${PAPERCLIP_API_BASE}/api/issues/${CEO_ALERT_ISSUE_ID}" 2>/dev/null)" || HTTP="000"
  if [ "$HTTP" = "200" ]; then
    pass "fallback CEO alert issue accessible (WIN-28, id=$CEO_ALERT_ISSUE_ID)"
  else
    fail "fallback CEO alert issue returned HTTP $HTTP — degraded backup unavailable"
  fi
else
  fail "PAPERCLIP_CEO_API_KEY not set — skipping fallback issue check"
fi

# ------------------------------------------------------------------
# 8. State file / data directory
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

if [ -f "$DATA_DIR/heartbeat.log" ]; then
  pass "heartbeat.log exists"
else
  warn "heartbeat.log not found yet (no direct wake has been launched from this machine)"
fi

# ------------------------------------------------------------------
# 9. Optional live wake test
# ------------------------------------------------------------------
section "Auto-wake trigger"

if [ "$TEST_WAKE" -eq 1 ]; then
  warn "Live wake test can cause Chief to process unread Telegram messages; clear the inbox first if you want a no-reply test."
  if [ -n "${PAPERCLIP_CEO_API_KEY:-}" ] && [ -n "$DIRECT_WAKE_CLI_KIND" ]; then
    PATH="$LAUNCHD_PATH_SETTING"
    ensure_launchd_node_path
    if [ "$DIRECT_WAKE_CLI_KIND" = "paperclipai" ]; then
      if paperclipai heartbeat run \
        --agent-id "$CEO_AGENT_ID" \
        --api-base "$PAPERCLIP_API_BASE" \
        --api-key "$PAPERCLIP_CEO_API_KEY" \
        --source automation \
        --trigger callback \
        --timeout-ms 15000 \
        --json >/dev/null 2>&1; then
        pass "Direct wake command executed successfully — inspect Chief activity/logs for the resulting heartbeat"
      else
        fail "Direct wake command failed"
      fi
    else
      if npx --yes paperclipai heartbeat run \
        --agent-id "$CEO_AGENT_ID" \
        --api-base "$PAPERCLIP_API_BASE" \
        --api-key "$PAPERCLIP_CEO_API_KEY" \
        --source automation \
        --trigger callback \
        --timeout-ms 15000 \
        --json >/dev/null 2>&1; then
        pass "Direct wake command executed successfully — inspect Chief activity/logs for the resulting heartbeat"
      else
        fail "Direct wake command failed"
      fi
    fi
    PATH="$ORIGINAL_PATH"
  else
    fail "Cannot run live wake test without a launchd-resolved direct-wake command and PAPERCLIP_CEO_API_KEY"
  fi
else
  warn "Skipped — re-run with --test-wake to fire a real direct wake and verify end-to-end"
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
