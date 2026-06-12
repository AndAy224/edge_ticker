#!/usr/bin/env bash
# Health watchdog, run every minute by ticker-watchdog.timer.
# - Restarts the backend after 3 consecutive failed health checks.
# - Restarts the kiosk when the backend is healthy but no display has been
#   connected for 5 consecutive checks (hung Chromium never exits, so
#   kiosk.service's Restart=always can't catch it).
set -u

FAIL_FILE=/run/ticker-watchdog.fails
NODISPLAY_FILE=/run/ticker-watchdog.nodisplay
BACKEND_THRESHOLD=3
DISPLAY_THRESHOLD=5

health=$(curl -fsS --max-time 10 http://127.0.0.1:8080/health 2>/dev/null)

if [ -z "$health" ]; then
  fails=$(($(cat "$FAIL_FILE" 2>/dev/null || echo 0) + 1))
  echo "$fails" > "$FAIL_FILE"
  if [ "$fails" -ge "$BACKEND_THRESHOLD" ]; then
    echo "ticker-watchdog: $fails consecutive health failures, restarting backend"
    systemctl restart ticker-backend.service
    rm -f "$FAIL_FILE"
  fi
  exit 0
fi
rm -f "$FAIL_FILE"

display=$(echo "$health" | python3 -c \
  "import json,sys; print(json.load(sys.stdin).get('display_clients', 1))" 2>/dev/null || echo 1)

if [ "$display" = "0" ]; then
  nodisplay=$(($(cat "$NODISPLAY_FILE" 2>/dev/null || echo 0) + 1))
  echo "$nodisplay" > "$NODISPLAY_FILE"
  if [ "$nodisplay" -ge "$DISPLAY_THRESHOLD" ]; then
    echo "ticker-watchdog: no display client for $nodisplay checks, restarting kiosk"
    systemctl restart kiosk.service
    rm -f "$NODISPLAY_FILE"
  fi
else
  rm -f "$NODISPLAY_FILE"
fi
