#!/usr/bin/env bash
# Update the appliance to origin/main — or back to the previous deploy.
# Run as root on the appliance:
#   sudo bash deploy/update.sh              # pull, verify, build, switch
#   sudo bash deploy/update.sh --rollback   # return to the commit before the last update
#
# Nothing live changes until the new code has passed its checks: the backend
# must import, the frontend must typecheck, and the bundle is built beside the
# live one and swapped in whole. After the switch the backend has to pass a
# health check, or the previous commit, its bundle, its Python packages and its
# own systemd units and udev rule are put back — as they are when any step of
# the switch itself fails, or the run is interrupted (Ctrl-C, a dropped SSH
# session, kill). A commit counts as deployed once that health check has
# passed, not because it is checked out. Changed systemd units and the udev
# rule are synced (install.sh used to be the only thing that copied them, so
# unit fixes never reached the appliance).
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
HEALTH=$BASE_URL/api/health
PY="$APP_DIR/.venv/bin/python"
# What a run leaves for the next one:
#   deployed           the commit whose switch last passed its health check
#   prev-commit        the commit before that one — where --rollback goes
#   dist.prev          the bundle that was live before the last switch (a bundle
#                      built here names its commit in .commit)
#   packages@<commit>  the venv's exact package set as that commit last ran
#   unfinished         only while a run is under way: the live commit, its target
#   packages.before    … and the venv's package set when that run began
STATE="$APP_DIR/.deploy"

cd "$APP_DIR"
mkdir -p "$STATE"
say() { echo "==> $*"; }
short() { git rev-parse --short "$1"; }

# Copy changed systemd units / udev rule into place. Sets KIOSK_CHANGED=1 /
# BACKEND_CHANGED=1 when that unit changed (it only takes effect on a restart).
KIOSK_CHANGED=0 BACKEND_CHANGED=0
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
      if [ "$unit" = ticker-backend.service ]; then BACKEND_CHANGED=1; fi
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

healthy() {
  for _ in $(seq 1 30); do
    curl -fsS --max-time 3 "$HEALTH" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

checked_out=$(git rev-parse HEAD)
deployed=$(cat "$STATE/deployed" 2>/dev/null || true)

# A run that was killed outright (power loss, kill -9) could restore nothing,
# but it left its record: the checkout may sit at its target while the commit
# it started from is still the one that runs — and the one to go back to. If
# something else has moved the checkout since, the record describes nothing.
prev=$checked_out resumed=0 was_live='' was_target=''
if [ -s "$STATE/unfinished" ]; then
  { read -r was_live && read -r was_target; } < "$STATE/unfinished" || was_live=''
  if [ -n "$was_live" ] && git cat-file -e "$was_live^{commit}" 2>/dev/null \
      && { [ "$checked_out" = "$was_target" ] || [ "$checked_out" = "$was_live" ]; }; then
    prev=$was_live resumed=1
    echo "!! an earlier run towards $(short "$was_target") never finished — $(short "$prev") is what was live"
  else
    rm -f "$STATE/unfinished" "$STATE/packages.before"
  fi
fi

rollback=0
if [ "${1:-}" = "--rollback" ]; then
  rollback=1
  [ -s "$STATE/prev-commit" ] || { echo "no previous deploy recorded"; exit 1; }
  target=$(cat "$STATE/prev-commit")
  if [ "$target" = "$prev" ]; then echo "already at the recorded previous commit"; exit 1; fi
  say "rolling back $(short "$prev") -> $(short "$target")"
else
  git fetch --quiet origin main
  target=$(git rev-parse origin/main)
  # Deployed means its switch passed the health check, not that HEAD is there:
  # a run that died in the build left HEAD at the target with the old backend
  # and bundle still live, and the next run called that "already at".
  if [ "$resumed" = 0 ] && [ "$target" = "$checked_out" ] && [ "$deployed" = "$target" ]; then
    say "already at $(short HEAD)"
    backend_unit="$ETC/systemd/system/ticker-backend.service"
    rm -f "$STATE/backend-unit.was"
    if [ -f "$backend_unit" ]; then cp "$backend_unit" "$STATE/backend-unit.was"; fi
    sync_system_files  # still useful: the first run of a new update.sh is the old one
    if [ "$BACKEND_CHANGED" = 1 ]; then
      # The copied unit used to wait for whatever restarted the backend next.
      # It takes effect now, and has to pass the health check a deploy does.
      say "ticker-backend.service changed — restarting the backend"
      if ! { systemctl restart ticker-backend.service && healthy; }; then
        echo "!! backend unhealthy under the changed unit"
        if [ -f "$STATE/backend-unit.was" ]; then
          echo "!! putting the unit it replaced back"
          cp "$STATE/backend-unit.was" "$backend_unit"
          systemctl daemon-reload
          systemctl restart ticker-backend.service || true
          if healthy; then echo "    backend is back up under it"; else echo "!! not healthy under that one either"; fi
        fi
        rm -f "$STATE/backend-unit.was"
        journalctl -u ticker-backend.service -n 30 --no-pager || true
        exit 1
      fi
    fi
    rm -f "$STATE/backend-unit.was"
    if [ "$KIOSK_CHANGED" = 1 ]; then systemctl restart kiosk.service; fi
    exit 0
  fi
  git merge-base --is-ancestor HEAD "$target" \
    || { echo "origin/main is not a fast-forward of $(short HEAD); refusing"; exit 1; }
  if [ "$target" != "$prev" ]; then
    say "updating $(short "$prev") -> $(short "$target")"
  else
    say "no finished deploy of $(short "$target") is recorded — verifying, building and switching it"
  fi
fi

python_deps() {
  "$PY" - <<'EOF'
import subprocess, sys, tomllib
deps = tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]
subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", *deps])
EOF
}

frozen() { "$PY" -m pip freeze; }
freeze_names() { sed -n -E 's/^([A-Za-z0-9][A-Za-z0-9._-]*)(==| @ ).*/\1/p'; }

# Make the venv hold exactly the packages of a `pip freeze` listing: its
# versions, and nothing that was installed since. Fails without the network
# when a version has to come back that pip no longer has cached.
install_exact() {
  local extra
  [ -s "$1" ] || return 1
  "$PY" -m pip install --quiet -r "$1" || return 1
  extra=$(frozen | freeze_names | grep -vxFf <(freeze_names < "$1") || true)
  if [ -n "$extra" ]; then
    # shellcheck disable=SC2086  # one name per word
    "$PY" -m pip uninstall --quiet --yes $extra || return 1
  fi
  frozen | cmp -s - "$1"
}

# Put the venv back as this run found it. The new dependencies go into the live
# venv before any check, and a restore used to reinstall the previous commit's
# `>=` list — which pip never downgrades for, so the old code came back on the
# very libraries the failed update had brought in.
deps_snapshot=0
restore_deps() {
  [ "$deps_snapshot" = 1 ] || return 0
  if frozen | cmp -s - "$STATE/packages.before"; then
    rm -f "$STATE/packages.before"
    return 0
  fi
  say "python dependencies: back to the previous set"
  if install_exact "$STATE/packages.before"; then
    rm -f "$STATE/packages.before"
  else
    mv -f "$STATE/packages.before" "$STATE/packages.unrestored"
    echo "!! PYTHON PACKAGES NOT RESTORED (no network?): $(short "$prev") is back, but on the"
    echo "!! packages this run installed. Once the network is back:"
    echo "!!   $PY -m pip install -r $STATE/packages.unrestored"
  fi
}

# Put the checkout back if anything before the switch fails or the run is
# interrupted. The running backend already has its code in memory and the live
# bundle is untouched. An SSH drop or Ctrl-C during the build used to kill the
# script where it stood: HEAD at the target, everything live still the old
# version, until the next backend restart booted the unverified code.
abort_to_prev() {
  # A failure inside $(…) runs the inherited trap in a subshell: fail just
  # that, and let the command that used it restore from the real shell.
  [ "$BASHPID" = "$$" ] || exit 1
  trap - ERR
  trap '' HUP INT TERM  # the restore itself is not to be interrupted
  set +e  # best effort: every restore step has to be tried
  echo "!! $1 before the switch — restoring $(short "$prev")"
  git reset --quiet --hard "$prev"
  rm -rf "$FRONTEND/dist.new"
  restore_deps
  rm -f "$STATE/unfinished"
  exit 1
}
trap 'abort_to_prev "update failed"' ERR
trap 'abort_to_prev "interrupted"' HUP INT TERM
# When the session goes, so may whatever the output was piped into (| tee):
# the next message — bash's own note that a step was killed, even — must fail
# as a write, not kill the script before it has restored anything.
trap '' PIPE

printf '%s\n%s\n' "$prev" "$target" > "$STATE/unfinished"
git reset --quiet --hard "$target"

say "python dependencies"
# A resumed run keeps the snapshot of the run that was killed: the venv may
# already hold what that one installed.
if [ "$resumed" = 0 ] || [ ! -s "$STATE/packages.before" ]; then
  frozen > "$STATE/packages.before.tmp"
  mv "$STATE/packages.before.tmp" "$STATE/packages.before"
fi
deps_snapshot=1
if [ "$rollback" = 1 ] && [ -s "$STATE/packages@$target" ]; then
  # The exact set this commit last ran with; its `>=` list alone would keep
  # every library the newer commit upgraded.
  if ! install_exact "$STATE/packages@$target"; then
    echo "!! could not reinstall the packages $(short "$target") last ran with (no network?) — keeping the current ones"
    python_deps
  fi
else
  python_deps
fi
say "backend import check"
"$PY" -c "import backend.main"

cd "$FRONTEND"
rm -rf dist.new
if [ "$rollback" = 1 ] && [ "$(cat "$STATE/dist.prev/.commit" 2>/dev/null || true)" = "$target" ]; then
  # The bundle this commit ran with is still on disk: a rollback used to
  # reinstall and rebuild it, which needs the network.
  say "frontend: the bundle saved from $(short "$target")"
  cp -a "$STATE/dist.prev" dist.new
else
  say "frontend: install, typecheck, build beside the live bundle"
  npm ci --no-audit --no-fund --loglevel=error
  npx tsc --noEmit
  npx vite build --outDir dist.new --emptyOutDir --logLevel warn
  echo "$target" > dist.new/.commit
fi
cd "$APP_DIR"

# From here the live system changes. The ERR trap used to be disarmed at this
# point, so a failing mv, cp into /etc or a backend that no longer started left
# the switch half done with nothing restored; and the failed-health rollback
# put the old code back under the NEW units — if the unit change was what broke
# it, the rollback failed too. Any failure or interruption from here on, and an
# unhealthy backend, restores the previous commit, its bundle, its packages and
# its own units and rule.
live_moved=0 new_placed=0
roll_back() {
  [ "$BASHPID" = "$$" ] || exit 1  # as in abort_to_prev
  trap - ERR
  trap '' HUP INT TERM
  set +e  # best effort: every restore step has to be tried
  echo "!! $1 — rolling back to $(short "$prev")"
  git reset --quiet --hard "$prev"
  if [ "$live_moved" = 1 ]; then
    rm -rf "$FRONTEND/dist"
    mv "$STATE/dist.prev" "$FRONTEND/dist"
  elif [ "$new_placed" = 1 ]; then
    rm -rf "$FRONTEND/dist"  # there was no bundle before this update
  fi
  rm -rf "$FRONTEND/dist.new"
  sync_system_files  # the previous commit's units and udev rule, now checked out
  restore_deps
  systemctl restart ticker-backend.service
  healthy && echo "    previous version is back up" || echo "!! previous version is not healthy either"
  journalctl -u ticker-backend.service -n 30 --no-pager
  rm -f "$STATE/unfinished"
  exit 1
}
trap 'roll_back "update failed during the switch"' ERR
trap 'roll_back "interrupted during the switch"' HUP INT TERM

say "switching"
if [ "$resumed" = 1 ] && [ "$was_target" != "$prev" ] && [ -d "$STATE/dist.prev" ] \
    && { [ ! -d "$FRONTEND/dist" ] \
         || [ "$(cat "$FRONTEND/dist/.commit" 2>/dev/null || true)" = "$was_target" ]; }; then
  # The killed run died inside its own switch: the bundle it moved aside is
  # still the one to go back to, and what is live (if anything) is its own.
  rm -rf "$FRONTEND/dist"
  live_moved=1
else
  rm -rf "$STATE/dist.prev"
  if [ -d "$FRONTEND/dist" ]; then mv "$FRONTEND/dist" "$STATE/dist.prev"; live_moved=1; fi
fi
mv "$FRONTEND/dist.new" "$FRONTEND/dist"
new_placed=1

sync_system_files

say "restarting backend"
systemctl restart ticker-backend.service
healthy || roll_back "backend unhealthy after the switch"
# The new version is up, and stays: a record that can't be written, or a kiosk
# restart or reload failing below, is no reason to take it down again (a run
# that ends without its records is finished by the next one).
trap - ERR
trap '' HUP INT TERM  # the records are written whole
# Recorded only once the new version is up, so a failed attempt can't make
# --rollback point at the version it is already running — nor can finishing a
# deploy of the commit that was already checked out.
if [ "$prev" != "$target" ]; then
  echo "$prev" > "$STATE/prev-commit"
  rm -f "$STATE"/packages@*
  mv "$STATE/packages.before" "$STATE/packages@$prev"
else
  rm -f "$STATE/packages.before"
fi
echo "$target" > "$STATE/deployed"
rm -f "$STATE/unfinished" "$STATE/packages.unrestored"
trap - HUP INT TERM PIPE

if [ "$KIOSK_CHANGED" = 1 ]; then
  say "kiosk.service changed — restarting the kiosk"
  systemctl restart kiosk.service
else
  # Displays reload themselves when a snapshot names a build they aren't
  # running. Bundles from before that existed need the reload action — once
  # the kiosk has reconnected, or the message reaches nobody.
  for _ in $(seq 1 30); do
    clients=$(curl -fsS --max-time 3 "$HEALTH" 2>/dev/null \
      | python3 -c 'import json,sys; h = json.load(sys.stdin); print(h.get("kiosk_clients", h.get("display_clients", 0)))' 2>/dev/null || echo 0)
    if [ "${clients:-0}" -ge 1 ]; then break; fi
    sleep 2
  done
  curl -fsS -X POST "$BASE_URL/api/control" -H 'Content-Type: application/json' \
    -d '{"action":"reload"}' >/dev/null || true
fi

if [ "$prev" != "$target" ]; then
  say "now at $(short HEAD) (previous: $(short "$prev"); undo with --rollback)"
else
  say "now at $(short HEAD), recorded as deployed"
fi
exit 0
}
