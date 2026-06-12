#!/usr/bin/env bash
# Health watchdog, run every minute by ticker-watchdog.timer.
# Restarts the backend after 3 consecutive failed health checks.
set -u

FAIL_FILE=/run/ticker-watchdog.fails
THRESHOLD=3

if curl -fsS --max-time 10 http://127.0.0.1:8080/health >/dev/null 2>&1; then
  rm -f "$FAIL_FILE"
  exit 0
fi

fails=$(($(cat "$FAIL_FILE" 2>/dev/null || echo 0) + 1))
echo "$fails" > "$FAIL_FILE"

if [ "$fails" -ge "$THRESHOLD" ]; then
  echo "ticker-watchdog: $fails consecutive health failures, restarting backend"
  systemctl restart ticker-backend.service
  rm -f "$FAIL_FILE"
fi
