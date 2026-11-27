#!/bin/bash
# run_job.sh <name> <script> <notify 0|1> <python> — written by install_mac_jobs.py
NAME="$1"; SCRIPT="$2"; NOTIFY="$3"; PY="$4"
cd "$HOME/boxandone" || exit 1
mkdir -p logs
LOG="logs/$NAME.log"
{
  echo ""
  echo "===== $NAME $(date '+%Y-%m-%d %H:%M:%S %Z') ($PY) ====="
  "$PY" -u "$SCRIPT" 2>&1 | grep --line-buffered -v -e PythonDeprecationWarning -e 'warnings.warn'
  echo "===== exit ${PIPESTATUS[0]} ====="
} >> "$LOG"
# keep the log to its last 5,000 lines
tail -n 5000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
if [ "$NOTIFY" = "1" ]; then
  LAST="$(grep -E 'done \(|EXCEPTION|another run started|exit [1-9]' "$LOG" | tail -n 1)"
  [ -z "$LAST" ] && LAST="finished — see logs/$NAME.log"
  /usr/bin/osascript -e "display notification \"${LAST//\"/}\" with title \"Box and One — $NAME\""
fi
