#!/usr/bin/env bash
# Health watchdog, run every minute by ticker-watchdog.timer.
# - Restarts the backend after 3 consecutive failed health checks.
# - Restarts the backend when a collector has stopped attempting polls (hung
#   fetch, dead loop — /health lists them in `stuck`) or a background task has
#   died (`dead_tasks`: no night schedule, no Home Assistant) for 3 consecutive
#   checks. A failing *upstream* is neither and never triggers a restart.
# - Restarts the kiosk when the backend is healthy but the kiosk's own display
#   socket has been missing for 5 consecutive checks (hung Chromium never
#   exits, so kiosk.service's Restart=always can't catch it).
# - Restarts the kiosk when its socket is there but the screen has stopped:
#   the page's pings carry a frame count (/health `kiosk`), and for 3 checks
#   it has not moved or the pings have stopped. That is the compositor's frame
#   loop dying under a live page — the panel showed one frozen picture for
#   hours, with every other check here passing, until someone power-cycled it.
#
# Restarts that don't help are not repeated forever: after MAX_STRIKES in a
# row without a sustained recovery in between, the condition is logged and
# left for a person (a collector that dies at every start would otherwise
# restart the backend every few minutes, each time re-polling rate-limited
# upstreams from cold; a panel that is switched off draws no frames either).
set -u

STATE_DIR=${WATCHDOG_STATE_DIR:-/run}  # overridable for tests
FAIL_FILE=$STATE_DIR/ticker-watchdog.fails
STUCK_FILE=$STATE_DIR/ticker-watchdog.stuck
STUCK_STRIKES_FILE=$STATE_DIR/ticker-watchdog.stuck-strikes
STUCK_CLEAR_FILE=$STATE_DIR/ticker-watchdog.stuck-clear
NODISPLAY_FILE=$STATE_DIR/ticker-watchdog.nodisplay
FROZEN_FILE=$STATE_DIR/ticker-watchdog.frozen
FROZEN_STRIKES_FILE=$STATE_DIR/ticker-watchdog.frozen-strikes
FROZEN_CLEAR_FILE=$STATE_DIR/ticker-watchdog.frozen-clear
BACKEND_THRESHOLD=3
STUCK_THRESHOLD=3
DISPLAY_THRESHOLD=5
FROZEN_THRESHOLD=3
# Seconds without a new frame (or a ping; they come every 10 s) before a check
# counts the panel as frozen, and how long a socket must have been connected
# before it is judged at all — a page that just (re)loaded is still starting.
FROZEN_AGE=60
KIOSK_GRACE=120
MAX_STRIKES=3
# Consecutive clean checks that count as recovered and forgive the strikes.
STUCK_CLEAR=30
FROZEN_CLEAR=10

# A counter file's value. Missing, empty or mangled reads as 0: `$(( ))` on
# anything else is an unbound-variable error under `set -u`, every minute.
count() {
  local n
  n=$(cat "$1" 2>/dev/null)
  case "$n" in
    '' | *[!0-9]*) echo 0 ;;
    *) echo "$n" ;;
  esac
}

bump() {
  local n=$(($(count "$1") + 1))
  echo "$n" > "$1" || echo "ticker-watchdog: cannot write $1" >&2
  echo "$n"
}

health=$(curl -fsS --max-time 10 http://127.0.0.1:8080/health 2>/dev/null)

if [ -z "$health" ]; then
  fails=$(bump "$FAIL_FILE")
  if [ "$fails" -ge "$BACKEND_THRESHOLD" ]; then
    echo "ticker-watchdog: $fails consecutive health failures, restarting backend"
    systemctl restart ticker-backend.service
    rm -f "$FAIL_FILE" "$FROZEN_FILE"
  fi
  exit 0
fi
rm -f "$FAIL_FILE"

# kiosk_clients counts only display sockets from this machine: display_clients
# also counts a /display tab left open on a laptop, which hid a hung kiosk.
# Backends from before kiosk_clients only report display_clients; ones from
# before `kiosk` say nothing about frames, and `frozen` stays "-" (unknown).
#
# An answer that cannot be read is an error here, not "all fine": this parse
# used to fall back to a connected kiosk with nothing stuck, so a /health that
# changed shape would have switched every check off without a word.
if ! parsed=$(printf '%s' "$health" | python3 -c '
import json, sys

age_limit, grace = float(sys.argv[1]), float(sys.argv[2])
h = json.load(sys.stdin)
if not isinstance(h, dict):
    sys.exit("not an object")


def number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def names(key):
    value = h.get(key) or []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        sys.exit(key + " is not a list of names")
    return ["_".join(v.split()) or "?" for v in value]


display = 1
for key in ("kiosk_clients", "display_clients"):
    if key in h:
        display = number(h[key])
        if display is None:
            sys.exit(key + " is not a number")
        break

unhealthy = names("stuck") + ["task:" + name for name in names("dead_tasks")]

frozen, detail = "-", "-"
kiosk = h.get("kiosk")
if isinstance(kiosk, dict):
    connected_for = number(kiosk.get("connected_for"))
    ages = {key: number(kiosk.get(key)) for key in ("frame_age", "ping_age")}
    ages = {key: age for key, age in ages.items() if age is not None}
    if connected_for is not None and connected_for >= grace and ages:
        worst = max(ages, key=ages.get)
        frozen = "1" if ages[worst] > age_limit else "0"
        detail = "%s=%.0fs" % (worst, ages[worst])

print(int(display), frozen, ",".join(unhealthy) or "-", detail)
' "$FROZEN_AGE" "$KIOSK_GRACE" 2>&1); then
  echo "ticker-watchdog: /health answered something unreadable, nothing checked: $parsed"
  exit 1
fi
read -r display frozen stuck detail <<< "$parsed"

if [ "$stuck" != "-" ]; then
  rm -f "$STUCK_CLEAR_FILE"
  stuck_count=$(bump "$STUCK_FILE")
  if [ "$stuck_count" -ge "$STUCK_THRESHOLD" ]; then
    rm -f "$STUCK_FILE"
    if [ "$(count "$STUCK_STRIKES_FILE")" -ge "$MAX_STRIKES" ]; then
      echo "ticker-watchdog: still stuck ($stuck) after $MAX_STRIKES backend restarts, leaving it"
    else
      echo "ticker-watchdog: stuck ($stuck) for $stuck_count checks, restarting backend"
      bump "$STUCK_STRIKES_FILE" > /dev/null
      systemctl restart ticker-backend.service
      # The kiosk reconnects to the new backend; what was read above is gone.
      rm -f "$FROZEN_FILE"
      exit 0
    fi
  fi
else
  rm -f "$STUCK_FILE"
  if [ -e "$STUCK_STRIKES_FILE" ] && [ "$(bump "$STUCK_CLEAR_FILE")" -ge "$STUCK_CLEAR" ]; then
    rm -f "$STUCK_STRIKES_FILE" "$STUCK_CLEAR_FILE"
  fi
fi

if [ "$display" = "0" ]; then
  rm -f "$FROZEN_FILE" "$FROZEN_CLEAR_FILE"
  nodisplay=$(bump "$NODISPLAY_FILE")
  if [ "$nodisplay" -ge "$DISPLAY_THRESHOLD" ]; then
    echo "ticker-watchdog: no kiosk display client for $nodisplay checks, restarting kiosk"
    systemctl restart kiosk.service
    rm -f "$NODISPLAY_FILE"
  fi
elif [ "$frozen" = "1" ]; then
  rm -f "$NODISPLAY_FILE" "$FROZEN_CLEAR_FILE"
  frozen_count=$(bump "$FROZEN_FILE")
  if [ "$frozen_count" -ge "$FROZEN_THRESHOLD" ]; then
    rm -f "$FROZEN_FILE"
    if [ "$(count "$FROZEN_STRIKES_FILE")" -ge "$MAX_STRIKES" ]; then
      echo "ticker-watchdog: screen still frozen ($detail) after $MAX_STRIKES kiosk restarts, leaving it"
    else
      echo "ticker-watchdog: kiosk connected but its screen is frozen ($detail) for $frozen_count checks, restarting kiosk"
      bump "$FROZEN_STRIKES_FILE" > /dev/null
      systemctl restart kiosk.service
    fi
  fi
else
  rm -f "$NODISPLAY_FILE" "$FROZEN_FILE"
  # "-" (too early to say, or a backend that doesn't report frames) is not
  # evidence of recovery; only frames seen moving are.
  if [ "$frozen" = "0" ] && [ -e "$FROZEN_STRIKES_FILE" ] \
    && [ "$(bump "$FROZEN_CLEAR_FILE")" -ge "$FROZEN_CLEAR" ]; then
    rm -f "$FROZEN_STRIKES_FILE" "$FROZEN_CLEAR_FILE"
  fi
fi
