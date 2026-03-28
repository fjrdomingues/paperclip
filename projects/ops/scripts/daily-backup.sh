#!/usr/bin/env bash
# Daily backup of the Paperclip org to GitHub.
# Stages all tracked + new files under the paperclip project directory,
# commits only if there are changes, and pushes to origin.

set -euo pipefail

REPO_ROOT="$HOME/Desktop/Projects/paperclip"
LOG_FILE="$REPO_ROOT/projects/ops/scripts/backup.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG_FILE"; }

cd "$REPO_ROOT"

# Pull latest to avoid non-fast-forward rejections
git pull --rebase origin main 2>/dev/null || log "Pull failed, continuing with local commit."

# Stage all tracked and new files
git add -A 2>/dev/null || true

# Check if there are staged changes
if git diff --cached --quiet; then
  log "No changes to back up."
  exit 0
fi

TIMESTAMP="$(date '+%Y-%m-%d %H:%M')"
git commit -m "Daily backup — $TIMESTAMP

Co-Authored-By: Paperclip <noreply@paperclip.ing>"

git push origin main

log "Backup committed and pushed successfully."
