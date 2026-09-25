#!/usr/bin/env bash
# Pull latest, rebuild, restart. Run as root on the appliance.
set -euo pipefail

APP_DIR=/opt/edge-ticker

git -C "$APP_DIR" pull --ff-only
cd "$APP_DIR"
"$APP_DIR/.venv/bin/python" - <<'EOF'
import subprocess, sys, tomllib
deps = tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]
subprocess.check_call([sys.executable, "-m", "pip", "install", *deps])
EOF
cd "$APP_DIR/frontend"
npm ci
npm run build
systemctl restart ticker-backend.service
# The display page reloads itself via the websocket reconnect; for a hard
# refresh use: curl -X POST localhost:8080/api/control -H 'Content-Type: application/json' -d '{"action":"reload"}'
echo "updated."
