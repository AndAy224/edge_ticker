#!/usr/bin/env bash
# Update the appliance to origin/main — or back to the previous deploy.
# Run as root on the appliance:
#   sudo bash deploy/update.sh              # pull, verify, build, switch
#   sudo bash deploy/update.sh --rollback   # return to the commit before the last update
#
# Nothing live changes until the new code has passed its checks: the backend
# must import, the frontend must typecheck, and the bundle is built beside the
# live one and swapped in whole. After the switch the backend has to pass a
# health check, or the previous commit, its bundle and its own systemd units
# and udev rule are put back — as they are when any step of the switch itself
# fails. Changed systemd units and the udev rule are synced (install.sh used to
# be the only thing that copied them, so unit fixes never reached the appliance).
# The whole script is one { … } block: bash reads a script as it runs, and the
# `git reset` below rewrites this very file — bash must have parsed all of it
# before that happens.
{
set -Eeuo pipefail  # -E: the ERR trap below must also fire inside functions (python_deps)

# The overrides exist to exercise this script in a sandbox checkout.
APP_DIR=${EDGE_TICKER_DIR:-/opt/edge-ticker}
ETC=${EDGE_TICKER_ETC:-/etc}
BASE_URL=${EDGE_TICKER_URL:-http://127.0.0.1:8080}
FRONTEND="$APP_DIR/frontend"
STATE="$APP_DIR/.deploy"          # previous commit + previous bundle
HEALTH=$BASE_URL/api/health

cd "$APP_DIR"
mkdir -p "$STATE"
say() { echo "==> $*"; }

# Copy changed systemd units / udev rule into place. Sets KIOSK_CHANGED=1 when
# kiosk.service changed (it only takes effect on a kiosk restart).
KIOSK_CHANGED=0
sync_system_files() {
  say "system units"
  local changed=0
  for unit in ticker-backend.service kiosk.service ticker-watchdog.service ticker-watchdog.timer; do
    # Rolling back past the commit that added a unit: nothing to put back.
    [ -f "$APP_DIR/deploy/$unit" ] || continue
    if ! cmp -s "$APP_DIR/deploy/$unit" "$ETC/systemd/system/$unit"; then
      cp "$APP_DIR/deploy/$unit" "$ETC/systemd/system/"
      echo "    updated $unit"
      changed=1
      if [ "$unit" = kiosk.service ]; then KIOSK_CHANGED=1; fi
    fi
  done
  if [ "$changed" = 1 ]; then
    systemctl daemon-reload
    systemctl restart ticker-watchdog.timer
  fi
  if ! cmp -s "$APP_DIR/deploy/99-edge-ticker-input.rules" "$ETC/udev/rules.d/99-edge-ticker-input.rules"; then
    cp "$APP_DIR/deploy/99-edge-ticker-input.rules" "$ETC/udev/rules.d/"
    udevadm control --reload-rules
    udevadm trigger --subsystem-match=input
    echo "    updated udev rule"
  fi
  chmod +x "$APP_DIR"/deploy/*.sh
}

prev=$(git rev-parse HEAD)
if [ "${1:-}" = "--rollback" ]; then
  [ -s "$STATE/prev-commit" ] || { echo "no previous deploy recorded"; exit 1; }
  target=$(cat "$STATE/prev-commit")
  if [ "$target" = "$prev" ]; then echo "already at the recorded previous commit"; exit 1; fi
  say "rolling back $(git rev-parse --short HEAD) -> $(git rev-parse --short "$target")"
else
  git fetch --quiet origin main
  target=$(git rev-parse origin/main)
  if [ "$target" = "$prev" ]; then
    say "already at $(git rev-parse --short HEAD)"
    sync_system_files  # still useful: the first run of a new update.sh is the old one
    if [ "$KIOSK_CHANGED" = 1 ]; then systemctl restart kiosk.service; fi
    exit 0
  fi
  git merge-base --is-ancestor HEAD "$target" \
    || { echo "origin/main is not a fast-forward of $(git rev-parse --short HEAD); refusing"; exit 1; }
  say "updating $(git rev-parse --short HEAD) -> $(git rev-parse --short "$target")"
fi

python_deps() {
  "$APP_DIR/.venv/bin/python" - <<'EOF'
import subprocess, sys, tomllib
deps = tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]
subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", *deps])
EOF
}

# Put the checkout back if anything before the switch fails. The running
# backend already has its code in memory and the live bundle is untouched.
abort_to_prev() {
  echo "!! update failed before the switch — restoring $(git rev-parse --short "$prev")"
  git reset --quiet --hard "$prev"
  rm -rf "$FRONTEND/dist.new"
}
trap abort_to_prev ERR

git reset --quiet --hard "$target"

say "python dependencies"
python_deps
say "backend import check"
"$APP_DIR/.venv/bin/python" -c "import backend.main"

say "frontend: install, typecheck, build beside the live bundle"
cd "$FRONTEND"
npm ci --no-audit --no-fund --loglevel=error
npx tsc --noEmit
rm -rf dist.new
npx vite build --outDir dist.new --emptyOutDir --logLevel warn
cd "$APP_DIR"

healthy() {
  for _ in $(seq 1 30); do
    curl -fsS --max-time 3 "$HEALTH" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

# From here the live system changes. The ERR trap used to be disarmed at this
# point, so a failing mv, cp into /etc or a backend that no longer started left
# the switch half done with nothing restored; and the failed-health rollback
# put the old code back under the NEW units — if the unit change was what broke
# it, the rollback failed too. Any failure from here on, and an unhealthy
# backend, restores the previous commit, its bundle and its own units and rule.
live_moved=0 new_placed=0
roll_back() {
  # A failure inside $(…) runs the inherited trap in a subshell: fail just
  # that, and let the command that used it roll back from the real shell.
  [ "$BASHPID" = "$$" ] || exit 1
  trap - ERR
  set +e  # best effort: every restore step has to be tried
  echo "!! $1 — rolling back to $(git rev-parse --short "$prev")"
  git reset --quiet --hard "$prev"
  if [ "$live_moved" = 1 ]; then
    rm -rf "$FRONTEND/dist"
    mv "$STATE/dist.prev" "$FRONTEND/dist"
  elif [ "$new_placed" = 1 ]; then
    rm -rf "$FRONTEND/dist"  # there was no bundle before this update
  fi
  rm -rf "$FRONTEND/dist.new"
  sync_system_files  # the previous commit's units and udev rule, now checked out
  python_deps
  systemctl restart ticker-backend.service
  healthy && echo "    previous version is back up" || echo "!! previous version is not healthy either"
  journalctl -u ticker-backend.service -n 30 --no-pager
  exit 1
}
trap 'roll_back "update failed during the switch"' ERR

say "switching"
rm -rf "$STATE/dist.prev"
if [ -d "$FRONTEND/dist" ]; then mv "$FRONTEND/dist" "$STATE/dist.prev"; live_moved=1; fi
mv "$FRONTEND/dist.new" "$FRONTEND/dist"
new_placed=1

sync_system_files

say "restarting backend"
systemctl restart ticker-backend.service
healthy || roll_back "backend unhealthy after the switch"
# Recorded only once the new version is up, so a failed attempt can't make
# --rollback point at the version it is already running.
echo "$prev" > "$STATE/prev-commit"
# The new version is up: a kiosk restart or reload failing below is no reason
# to take it down again.
trap - ERR

if [ "$KIOSK_CHANGED" = 1 ]; then
  say "kiosk.service changed — restarting the kiosk"
  systemctl restart kiosk.service
else
  # Displays reload themselves when a snapshot names a build they aren't
  # running. Bundles from before that existed need the reload action — once
  # the kiosk has reconnected, or the message reaches nobody.
  for _ in $(seq 1 30); do
    clients=$(curl -fsS --max-time 3 "$HEALTH" 2>/dev/null \
      | python3 -c 'import json,sys; print(json.load(sys.stdin).get("display_clients", 0))' 2>/dev/null || echo 0)
    if [ "${clients:-0}" -ge 1 ]; then break; fi
    sleep 2
  done
  curl -fsS -X POST "$BASE_URL/api/control" -H 'Content-Type: application/json' \
    -d '{"action":"reload"}' >/dev/null || true
fi

say "now at $(git rev-parse --short HEAD) (previous: $(git rev-parse --short "$prev"); undo with --rollback)"
exit 0
}
