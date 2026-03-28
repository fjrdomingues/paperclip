#!/usr/bin/env bash
# Deploy viewer code to production server.
# Only rebuilds Docker container when code changes are detected.
# Runs via LaunchAgent (com.paperclip.whatsapp-deploy).

set -euo pipefail

SERVER="root@64.226.74.167"
REMOTE_APP="/root/apps/whatsapp-viewer"
LOCAL_BASE="/Users/fabiodomingues/Desktop/Projects/paperclip/projects/whatsapp"
LOG="$LOCAL_BASE/data/deploy.log"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$LOG"; }

# Sync code files, capturing itemized changes to detect if anything updated
CHANGES=$(/usr/bin/rsync -az --itemize-changes --timeout=30 \
  "$LOCAL_BASE/viewer/app.py" \
  "$LOCAL_BASE/viewer/requirements.txt" \
  "$LOCAL_BASE/db.py" \
  "$SERVER:$REMOTE_APP/" 2>&1) || { log "ERROR: rsync app files failed"; exit 1; }

# Sync template files individually (avoids macOS TCC opendir restriction)
TMPL_CHANGES=$(/usr/bin/rsync -az --itemize-changes --timeout=30 \
  "$LOCAL_BASE/viewer/templates/index.html" \
  "$LOCAL_BASE/viewer/templates/dashboard.html" \
  "$SERVER:$REMOTE_APP/templates/" 2>&1) || { log "ERROR: rsync templates failed"; exit 1; }

ALL_CHANGES="${CHANGES}${TMPL_CHANGES}"

if [ -z "$ALL_CHANGES" ]; then
  log "no code changes detected, skipping rebuild"
  exit 0
fi

log "code changes detected, rebuilding container"
log "changed files: $(echo "$ALL_CHANGES" | grep '^>' | awk '{print $2}' | tr '\n' ' ')"

ssh -o ConnectTimeout=15 "$SERVER" \
  "cd $REMOTE_APP && docker compose build --quiet && docker compose up -d" \
  >> "$LOG" 2>&1 || { log "ERROR: docker rebuild failed"; exit 1; }

log "deploy complete"
