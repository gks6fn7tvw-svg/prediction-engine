#!/usr/bin/env bash
# Turn the bot into a background service: it starts when the server starts
# and restarts by itself if it ever crashes. Run from the repo folder:
#   bash btcbot/deploy/install_service.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
USER_NAME="$(whoami)"
ENV_FILE="$HOME/.btcbot.env"   # holds the Kalshi key details for live mode

if [ ! -x "$REPO/.venv/bin/python" ]; then
  echo "First run the install steps in btcbot/README.md (the .venv folder is missing)."
  exit 1
fi
if [ ! -f "$REPO/btcbot/settings.yaml" ]; then
  cp "$REPO/btcbot/settings.example.yaml" "$REPO/btcbot/settings.yaml"
  echo "Made btcbot/settings.yaml from the example (paper mode)."
fi
touch "$ENV_FILE"
chmod 600 "$ENV_FILE"

sudo tee /etc/systemd/system/btcbot.service >/dev/null <<EOF
[Unit]
Description=Kalshi 15-minute bot
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$REPO
EnvironmentFile=-$ENV_FILE
ExecStart=$REPO/.venv/bin/python -m btcbot run
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now btcbot
echo
echo "The bot is running. Watch it with:   journalctl -u btcbot -f"
echo "(press Ctrl+C to stop watching; the bot keeps running)"
