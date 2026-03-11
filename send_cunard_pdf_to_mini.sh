#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 /path/to/daily-programme.pdf"
  exit 1
fi

PDF_PATH="$1"
MINI_HOST="Nudge@100.115.36.101"
REMOTE_INBOX="/Users/nudge/clawd/inbox/cunard/incoming"
REMOTE_SKILL_DIR="/Users/nudge/clawd/skills/cunard-reminders"
REMOTE_SCRIPT="$REMOTE_SKILL_DIR/process_cunard_inbox.py"

if [[ ! -f "$PDF_PATH" ]]; then
  echo "PDF not found: $PDF_PATH"
  exit 1
fi

echo "Uploading $PDF_PATH to $MINI_HOST:$REMOTE_INBOX ..."
ssh "$MINI_HOST" "mkdir -p '$REMOTE_INBOX'"
scp "$PDF_PATH" "$MINI_HOST:$REMOTE_INBOX/"

echo "Triggering remote processing ..."
ssh "$MINI_HOST" "cd '$REMOTE_SKILL_DIR' && python3 '$REMOTE_SCRIPT'"

echo "Done."
