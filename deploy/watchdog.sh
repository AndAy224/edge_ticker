#!/usr/bin/env bash
# Health watchdog, run every minute by ticker-watchdog.timer.
# - Restarts the backend after 3 consecutive failed health checks.
# - Restarts the backend when a collector has stopped attempting polls (hung
#   fetch, dead loop — /health lists them in `stuck`) for 3 consecutive
#   checks. A failing *upstream* is not stuck and never triggers a restart.
# - Restarts the kiosk when the backend is healthy but no display has been
#   connected for 5 consecutive checks (hung Chromium never exits, so
#   kiosk.service's Restart=always can't catch it).
set -u

FAIL_FILE=/run/ticker-watchdog.fails
STUCK_FILE=/run/ticker-watchdog.stuck
NODISPLAY_FILE=/run/ticker-watchdog.nodisplay
BACKEND_THRESHOLD=3
STUCK_THRESHOLD=3
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

read -r display stuck < <(echo "$health" | python3 -c '
import json, sys
h = json.load(sys.stdin)
print(h.get("display_clients", 1), ",".join(h.get("stuck") or []) or "-")
' 2>/dev/null || echo "1 -")

if [ "${stuck:--}" != "-" ]; then
  stuck_count=$(($(cat "$STUCK_FILE" 2>/dev/null || echo 0) + 1))
  echo "$stuck_count" > "$STUCK_FILE"
  if [ "$stuck_count" -ge "$STUCK_THRESHOLD" ]; then
    echo "ticker-watchdog: collectors stuck ($stuck) for $stuck_count checks, restarting backend"
    systemctl restart ticker-backend.service
    rm -f "$STUCK_FILE"
    exit 0
  fi
else
  rm -f "$STUCK_FILE"
fi

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
