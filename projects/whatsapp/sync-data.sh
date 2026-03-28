#!/usr/bin/env bash
# Sync campaign data files to production server for the dashboard.
# Runs hourly via LaunchAgent.

set -euo pipefail

SERVER="root@64.226.74.167"
REMOTE_BASE="/root/apps/whatsapp-viewer/sync-data"
LOCAL_BASE="/Users/fabiodomingues/Desktop/Projects/paperclip"

# Sync WhatsApp data (sent_log.csv, inbox.jsonl)
rsync -az --timeout=30 \
  "$LOCAL_BASE/projects/whatsapp/data/sent_log.csv" \
  "$LOCAL_BASE/projects/whatsapp/data/inbox.jsonl" \
  "$LOCAL_BASE/projects/whatsapp/data/whatsapp.db" \
  "$SERVER:$REMOTE_BASE/whatsapp/"

# Sync growth data (leads.csv)
rsync -az --timeout=30 \
  "$LOCAL_BASE/projects/growth/data/leads.csv" \
  "$SERVER:$REMOTE_BASE/growth/"

echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) sync complete" >> "$LOCAL_BASE/projects/whatsapp/data/sync.log"
