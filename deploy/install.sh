#!/usr/bin/env bash
# Idempotent setup for a fresh Ubuntu Server 24.04 kiosk host.
# Run as root: sudo bash deploy/install.sh
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/AndAy224/edge-ticker.git}"
APP_DIR=/opt/edge-ticker

echo "==> apt packages"
apt-get update
apt-get install -y cage chromium-browser ddcutil git python3-venv npm libinput-tools

echo "==> users"
id -u ticker &>/dev/null || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin ticker
id -u kiosk &>/dev/null || useradd --create-home --shell /usr/sbin/nologin kiosk
usermod -aG video,input,render kiosk
usermod -aG i2c ticker 2>/dev/null || true   # ddcutil access for scheduled dimming

echo "==> repo"
if [ -d "$APP_DIR/.git" ]; then
  git -C "$APP_DIR" pull --ff-only
else
  git clone "$REPO_URL" "$APP_DIR"
fi

echo "==> python env"
cd "$APP_DIR"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install --upgrade pip
# App layout, not a package: install the dependency list, run from the repo.
.venv/bin/python - <<'EOF'
import subprocess, sys, tomllib
deps = tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]
subprocess.check_call([sys.executable, "-m", "pip", "install", *deps])
EOF

echo "==> frontend build"
cd "$APP_DIR/frontend"
npm ci
npm run build

echo "==> env file"
if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo "    NOTE: edit $APP_DIR/.env with your HA_URL / HA_TOKEN"
fi
chown root:ticker "$APP_DIR/.env"
chmod 640 "$APP_DIR/.env"
mkdir -p "$APP_DIR/data"
chown -R ticker:ticker "$APP_DIR/data"

echo "==> systemd units"
cp "$APP_DIR/deploy/ticker-backend.service" /etc/systemd/system/
cp "$APP_DIR/deploy/kiosk.service" /etc/systemd/system/
cp "$APP_DIR/deploy/ticker-watchdog.service" /etc/systemd/system/
cp "$APP_DIR/deploy/ticker-watchdog.timer" /etc/systemd/system/
chmod +x "$APP_DIR/deploy/watchdog.sh" "$APP_DIR/deploy/update.sh"
systemctl daemon-reload
systemctl enable --now ticker-backend.service
systemctl enable --now ticker-watchdog.timer
systemctl enable kiosk.service

echo "==> disable console blanking & suspend"
systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
if ! grep -q consoleblank /etc/default/grub; then
  sed -i 's/GRUB_CMDLINE_LINUX_DEFAULT="/GRUB_CMDLINE_LINUX_DEFAULT="consoleblank=0 /' /etc/default/grub
  update-grub
fi

echo "==> done. Start the kiosk with: systemctl start kiosk.service"
