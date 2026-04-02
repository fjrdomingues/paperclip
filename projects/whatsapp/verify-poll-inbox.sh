#!/bin/bash
# verify-poll-inbox.sh — Safe local verification for poll-inbox.sh/run-poll.py.
# Exercises the DST cursor repro, raw JSONL SID idempotency, and SQLite sync on temp data.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
POLLER="$SCRIPT_DIR/poll-inbox.sh"
RUN_POLL="$SCRIPT_DIR/run-poll.py"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

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

assert_eq() {
  local actual="$1"
  local expected="$2"
  local message="$3"
  if [ "$actual" = "$expected" ]; then
    pass "$message"
  else
    fail "$message (expected=$expected actual=$actual)"
  fi
}

iso_epoch_utc_python() {
  python3 - "$1" <<'PY'
import datetime
import sys

value = sys.argv[1]
dt = datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
print(int(dt.timestamp()))
PY
}

run_poller() {
  local case_dir="$1"
  local mock_response_path="$case_dir/response.json"
  if [ -d "$case_dir/response-pages" ]; then
    mock_response_path="$case_dir/response-pages"
  fi
  TWILIO_ACCOUNT_SID=test-account \
  TWILIO_API_KEY_SID=test-key \
  TWILIO_API_KEY_SECRET=test-secret \
  WHATSAPP_POLL_DATA_DIR="$case_dir/data" \
  WHATSAPP_POLL_LOG_FILE="$case_dir/data/poll.log" \
  WHATSAPP_POLL_MOCK_RESPONSE_FILE="$mock_response_path" \
  "$POLLER"
}

run_wrapper() {
  local case_dir="$1"
  local mock_response_path="$case_dir/response.json"
  if [ -d "$case_dir/response-pages" ]; then
    mock_response_path="$case_dir/response-pages"
  fi
  TWILIO_ACCOUNT_SID=test-account \
  TWILIO_API_KEY_SID=test-key \
  TWILIO_API_KEY_SECRET=test-secret \
  WHATSAPP_POLL_DATA_DIR="$case_dir/data" \
  WHATSAPP_POLL_LOG_FILE="$case_dir/data/poll.log" \
  WHATSAPP_POLL_MOCK_RESPONSE_FILE="$mock_response_path" \
  WHATSAPP_DB_PATH="$case_dir/data/whatsapp.db" \
  python3 "$RUN_POLL"
}

count_inbox_lines() {
  local case_dir="$1"
  if [ -f "$case_dir/data/inbox.jsonl" ]; then
    wc -l < "$case_dir/data/inbox.jsonl" | tr -d ' '
  else
    echo 0
  fi
}

count_sid_occurrences() {
  local case_dir="$1"
  local sid="$2"
  if [ -f "$case_dir/data/inbox.jsonl" ]; then
    jq -r '.sid // empty' "$case_dir/data/inbox.jsonl" | awk -v sid="$sid" '$0 == sid {count++} END {print count+0}'
  else
    echo 0
  fi
}

count_unique_sids() {
  local case_dir="$1"
  if [ -f "$case_dir/data/inbox.jsonl" ]; then
    jq -r '.sid // empty' "$case_dir/data/inbox.jsonl" | sed '/^$/d' | sort -u | wc -l | tr -d ' '
  else
    echo 0
  fi
}

state_field() {
  local case_dir="$1"
  local field="$2"
  jq -r --arg field "$field" '.[$field] // ""' "$case_dir/data/state.json"
}

printf "\nDST cursor repro\n"
CASE1_DIR="$TMP_DIR/case1"
mkdir -p "$CASE1_DIR/data"
jq -n \
  --arg last_message_sid "" \
  --arg last_poll "2026-03-30T11:34:03Z" \
  '{"last_message_sid": $last_message_sid, "last_poll": $last_poll}' > "$CASE1_DIR/data/state.json"
cat > "$CASE1_DIR/response.json" <<'JSON'
{
  "messages": [
    {
      "sid": "SMold",
      "direction": "inbound",
      "date_sent": "Mon, 30 Mar 2026 11:00:00 +0000",
      "from": "whatsapp:+351900000001",
      "body": "already processed before DST fix"
    },
    {
      "sid": "SMfresh",
      "direction": "inbound",
      "date_sent": "Mon, 30 Mar 2026 12:00:00 +0000",
      "from": "whatsapp:+351900000002",
      "body": "new inbound after cursor"
    }
  ]
}
JSON

BUGGY_EPOCH="$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "2026-03-30T11:34:03Z" "+%s")"
FIXED_EPOCH="$(date -u -j -f "%Y-%m-%dT%H:%M:%SZ" "2026-03-30T11:34:03Z" "+%s")"
EXPECTED_EPOCH="$(iso_epoch_utc_python "2026-03-30T11:34:03Z")"
assert_eq "$(( FIXED_EPOCH - BUGGY_EPOCH ))" "3600" "UTC parse fixes the one-hour DST skew"
assert_eq "$FIXED_EPOCH" "$EXPECTED_EPOCH" "fixed parse matches Python UTC epoch"

run_poller "$CASE1_DIR"
assert_eq "$(count_inbox_lines "$CASE1_DIR")" "1" "poller keeps only messages after the UTC cursor"
assert_eq "$(count_sid_occurrences "$CASE1_DIR" "SMold")" "0" "older SID is skipped after UTC cursor fix"
assert_eq "$(count_sid_occurrences "$CASE1_DIR" "SMfresh")" "1" "newer SID is appended once"

printf "\nRaw inbox idempotency\n"
CASE2_DIR="$TMP_DIR/case2"
mkdir -p "$CASE2_DIR/data"
jq -nc \
  --arg from "+351900000003" \
  --arg body "existing inbound" \
  --arg timestamp "2026-03-30T12:00:00Z" \
  --arg sid "SMexisting" \
  --arg status "received" \
  '{from: $from, body: $body, timestamp: $timestamp, sid: $sid, status: $status}' \
  > "$CASE2_DIR/data/inbox.jsonl"
jq -n \
  --arg last_message_sid "" \
  --arg last_poll "2026-03-30T10:00:00Z" \
  '{"last_message_sid": $last_message_sid, "last_poll": $last_poll}' > "$CASE2_DIR/data/state.json"
cat > "$CASE2_DIR/response.json" <<'JSON'
{
  "messages": [
    {
      "sid": "SMexisting",
      "direction": "inbound",
      "date_sent": "Mon, 30 Mar 2026 12:00:00 +0000",
      "from": "whatsapp:+351900000003",
      "body": "existing inbound"
    },
    {
      "sid": "SMfresh2",
      "direction": "inbound",
      "date_sent": "Mon, 30 Mar 2026 12:05:00 +0000",
      "from": "whatsapp:+351900000004",
      "body": "brand new inbound"
    }
  ]
}
JSON

run_poller "$CASE2_DIR"
assert_eq "$(count_inbox_lines "$CASE2_DIR")" "2" "duplicate SID is not re-appended to inbox.jsonl"
assert_eq "$(count_unique_sids "$CASE2_DIR")" "2" "raw inbox preserves unique SIDs only"
assert_eq "$(count_sid_occurrences "$CASE2_DIR" "SMexisting")" "1" "existing SID remains single-copy"
assert_eq "$(count_sid_occurrences "$CASE2_DIR" "SMfresh2")" "1" "new SID is appended once"

printf "\nCursor recovery and same-second boundary\n"
CASE3_DIR="$TMP_DIR/case3"
mkdir -p "$CASE3_DIR/data"
jq -nc \
  --arg from "+351918416949" \
  --arg body "already stored inbound" \
  --arg timestamp "2026-03-31T11:22:30Z" \
  --arg sid "SMstored" \
  --arg status "received" \
  '{from: $from, body: $body, timestamp: $timestamp, sid: $sid, status: $status}' \
  > "$CASE3_DIR/data/inbox.jsonl"
jq -n \
  --arg last_message_sid "SMstored" \
  --arg last_poll "2026-03-31T14:50:00Z" \
  '{"last_message_sid": $last_message_sid, "last_poll": $last_poll}' > "$CASE3_DIR/data/state.json"
cat > "$CASE3_DIR/response-empty.json" <<'JSON'
{
  "messages": [
    {
      "sid": "SMstored",
      "direction": "inbound",
      "date_sent": "Tue, 31 Mar 2026 11:22:30 +0000",
      "from": "whatsapp:+351918416949",
      "body": "already stored inbound"
    }
  ]
}
JSON
cat > "$CASE3_DIR/response-late.json" <<'JSON'
{
  "messages": [
    {
      "sid": "SMstored",
      "direction": "inbound",
      "date_sent": "Tue, 31 Mar 2026 11:22:30 +0000",
      "from": "whatsapp:+351918416949",
      "body": "already stored inbound"
    },
    {
      "sid": "SMsame-second",
      "direction": "inbound",
      "date_sent": "Tue, 31 Mar 2026 11:22:30 +0000",
      "from": "whatsapp:+351918416950",
      "body": "same second, different sid"
    },
    {
      "sid": "SMlate",
      "direction": "inbound",
      "date_sent": "Tue, 31 Mar 2026 11:22:51 +0000",
      "from": "whatsapp:+351918416951",
      "body": "late visible inbound"
    }
  ]
}
JSON

cp "$CASE3_DIR/response-empty.json" "$CASE3_DIR/response.json"
run_poller "$CASE3_DIR"
assert_eq "$(state_field "$CASE3_DIR" "last_poll")" "2026-03-31T11:22:30Z" "stale cursor clamps back to the last stored SID timestamp"

cp "$CASE3_DIR/response-late.json" "$CASE3_DIR/response.json"
run_poller "$CASE3_DIR"
assert_eq "$(count_sid_occurrences "$CASE3_DIR" "SMstored")" "1" "existing boundary SID stays deduped"
assert_eq "$(count_sid_occurrences "$CASE3_DIR" "SMsame-second")" "1" "same-second unseen SID is still ingested"
assert_eq "$(count_sid_occurrences "$CASE3_DIR" "SMlate")" "1" "late-visible inbound is recovered after cursor clamp"
assert_eq "$(state_field "$CASE3_DIR" "last_message_sid")" "SMlate" "state tracks the newest recovered SID"
assert_eq "$(state_field "$CASE3_DIR" "last_poll")" "2026-03-31T11:22:51Z" "cursor advances to the newest ingested timestamp"

printf "\nPaginated anchor recovery\n"
CASE4_DIR="$TMP_DIR/case4"
mkdir -p "$CASE4_DIR/data" "$CASE4_DIR/response-pages"
jq -nc \
  --arg from "+351918416952" \
  --arg body "already stored anchor" \
  --arg timestamp "2026-03-31T10:00:00Z" \
  --arg sid "SMpage-anchor" \
  --arg status "received" \
  '{from: $from, body: $body, timestamp: $timestamp, sid: $sid, status: $status}' \
  > "$CASE4_DIR/data/inbox.jsonl"
jq -n \
  --arg last_message_sid "SMpage-anchor" \
  --arg last_poll "2026-03-31T14:00:00Z" \
  '{"last_message_sid": $last_message_sid, "last_poll": $last_poll}' > "$CASE4_DIR/data/state.json"
cat > "$CASE4_DIR/response-pages/page-1.json" <<'JSON'
{
  "messages": [
    {
      "sid": "SMpage-same-second",
      "direction": "inbound",
      "date_sent": "Tue, 31 Mar 2026 10:00:00 +0000",
      "from": "whatsapp:+351918416953",
      "body": "same-second unseen message"
    },
    {
      "sid": "SMpage-late",
      "direction": "inbound",
      "date_sent": "Tue, 31 Mar 2026 10:05:00 +0000",
      "from": "whatsapp:+351918416954",
      "body": "later message on newest page"
    }
  ],
  "next_page_uri": "/2010-04-01/Accounts/test-account/Messages.json?PageToken=page2"
}
JSON
cat > "$CASE4_DIR/response-pages/page-2.json" <<'JSON'
{
  "messages": [
    {
      "sid": "SMpage-anchor",
      "direction": "inbound",
      "date_sent": "Tue, 31 Mar 2026 10:00:00 +0000",
      "from": "whatsapp:+351918416952",
      "body": "already stored anchor"
    }
  ],
  "next_page_uri": null
}
JSON

run_poller "$CASE4_DIR"
assert_eq "$(count_sid_occurrences "$CASE4_DIR" "SMpage-anchor")" "1" "anchor SID is not re-appended when it only appears on a later page"
assert_eq "$(count_sid_occurrences "$CASE4_DIR" "SMpage-same-second")" "1" "same-second unseen SID is recovered across Twilio pages"
assert_eq "$(count_sid_occurrences "$CASE4_DIR" "SMpage-late")" "1" "newer inbound message is recovered across Twilio pages"
assert_eq "$(state_field "$CASE4_DIR" "last_message_sid")" "SMpage-late" "paginated recovery keeps newest SID in state"
assert_eq "$(state_field "$CASE4_DIR" "last_poll")" "2026-03-31T10:05:00Z" "paginated recovery clamps and advances the cursor"

printf "\nlast_run freshness signal\n"
CASE5_DIR="$TMP_DIR/case5"
mkdir -p "$CASE5_DIR/data"
jq -n \
  --arg last_message_sid "" \
  --arg last_poll "2026-04-01T10:00:00Z" \
  '{"last_message_sid": $last_message_sid, "last_poll": $last_poll}' > "$CASE5_DIR/data/state.json"
cat > "$CASE5_DIR/response.json" <<'JSON'
{"messages": []}
JSON
run_poller "$CASE5_DIR"
LAST_RUN="$(state_field "$CASE5_DIR" "last_run")"
if [ -n "$LAST_RUN" ]; then
  pass "last_run is written on every successful poll cycle"
else
  fail "last_run missing from state.json after poll with no new messages"
fi
# last_poll must not advance when no new messages arrive
assert_eq "$(state_field "$CASE5_DIR" "last_poll")" "2026-04-01T10:00:00Z" "last_poll cursor unchanged when no new messages"

printf "\nSQLite sync\n"
run_wrapper "$CASE2_DIR"
DB_COUNT="$(python3 - "$CASE2_DIR/data/whatsapp.db" <<'PY'
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1])
count = conn.execute("SELECT COUNT(*) FROM inbound_messages").fetchone()[0]
print(count)
conn.close()
PY
)"
assert_eq "$DB_COUNT" "2" "run-poll.py still syncs deduped raw inbox into SQLite"

printf "\nSummary: %d passed, %d failed\n" "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
