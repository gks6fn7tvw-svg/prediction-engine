#!/usr/bin/env bash
# Google Cloud "startup script" setup: installs and starts the bot with no SSH.
# The server runs this every time it boots (as root). Put these two lines in the
# server's Startup script box (Edit > Advanced/Management > Automation):
#
#   #!/bin/bash
#   (curl -fsSL https://raw.githubusercontent.com/gks6fn7tvw-svg/prediction-engine/btcbot/btcbot/deploy/gce_startup.sh || curl -fsSL https://raw.githubusercontent.com/gks6fn7tvw-svg/prediction-engine/main/btcbot/deploy/gce_startup.sh) | bash
#
# Progress and the bot's log show up in the server page under
# Logs > Serial port 1 (console). So does the private website link
# (search for "BTCBOT WEBSITE"); tick "Allow HTTP traffic" on the server once.
set -uo pipefail

URL="https://github.com/gks6fn7tvw-svg/prediction-engine.git"
REPO=/opt/prediction-engine
RUN_AS=btcbot
META="http://metadata.google.internal/computeMetadata/v1/instance/attributes"
IP_URL="http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip"
KEY_FILE=/var/lib/btcbot/web.key
say() { echo "BTCBOT SETUP: $*"; }
as_bot() { runuser -u "$RUN_AS" -- "$@"; }

say "starting"
command -v git >/dev/null && python3 -c "import venv, ensurepip" 2>/dev/null || {
  say "installing git and python"
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3-venv \
    || { say "FAILED to install git/python"; exit 1; }
}

id -u "$RUN_AS" >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/btcbot \
  --shell /usr/sbin/nologin "$RUN_AS"

# Use the btcbot branch until it is merged, then main.
BRANCH=btcbot
git ls-remote --exit-code --heads "$URL" btcbot >/dev/null 2>&1 || BRANCH=main
if [ ! -d "$REPO/.git" ]; then
  say "downloading the bot ($BRANCH)"
  git clone --depth 1 -b "$BRANCH" "$URL" "$REPO" || { say "FAILED to download"; exit 1; }
  chown -R "$RUN_AS:$RUN_AS" "$REPO"
else
  say "getting the newest code ($BRANCH)"
  as_bot git -C "$REPO" fetch --depth 1 origin "$BRANCH" \
    && as_bot git -C "$REPO" checkout -q -f -B "$BRANCH" FETCH_HEAD \
    || say "could not update the code; keeping the copy already here"
fi

if [ ! -x "$REPO/.venv/bin/python" ]; then
  say "setting up python (a few minutes)"
  as_bot python3 -m venv "$REPO/.venv" || { say "FAILED to make the python folder"; exit 1; }
fi
as_bot "$REPO/.venv/bin/pip" install -q -r "$REPO/btcbot/requirements.txt" \
  || { say "FAILED to install python packages"; exit 1; }

# Optional: settings pasted into the server's metadata as key "btcbot-settings"
# replace settings.yaml on every boot. Leave it unset when using the website,
# or website changes are replaced at the next boot.
SETTINGS="$REPO/btcbot/settings.yaml"
if curl -fs -H "Metadata-Flavor: Google" "$META/btcbot-settings" -o /tmp/btcbot-settings.yaml \
    && [ -s /tmp/btcbot-settings.yaml ]; then
  say "using settings from the server's metadata (btcbot-settings)"
  install -o "$RUN_AS" -g "$RUN_AS" -m 644 /tmp/btcbot-settings.yaml "$SETTINGS"
elif [ ! -f "$SETTINGS" ]; then
  as_bot cp "$REPO/btcbot/settings.example.yaml" "$SETTINGS"
  say "made settings.yaml from the example (paper mode)"
fi
rm -f /tmp/btcbot-settings.yaml

touch /etc/btcbot.env && chmod 600 /etc/btcbot.env   # Kalshi key details go here for live mode

# Real money, with no SSH: add these to the server's metadata (Edit > Metadata), then Reset.
#   kalshi-key-id       your Kalshi API Key ID
#   kalshi-private-key  the whole text of the private key file Kalshi gave you
#   btcbot-mode         live (real money) or paper (pretend money)
# The key file is saved where only the bot can read it.
KEY_ID=$(curl -fs -H "Metadata-Flavor: Google" "$META/kalshi-key-id" || true)
if [ -n "$KEY_ID" ]; then
  if curl -fs -H "Metadata-Flavor: Google" "$META/kalshi-private-key" -o /tmp/kalshi_key.pem \
      && grep -q "PRIVATE KEY" /tmp/kalshi_key.pem; then
    install -o "$RUN_AS" -g "$RUN_AS" -m 600 /tmp/kalshi_key.pem /var/lib/btcbot/kalshi_key.pem
    printf 'KALSHI_API_KEY_ID=%s\nKALSHI_PRIVATE_KEY_PATH=/var/lib/btcbot/kalshi_key.pem\n' "$KEY_ID" \
      > /etc/btcbot.env
    say "Kalshi key loaded from the server's metadata"
  else
    say "kalshi-key-id is set, but kalshi-private-key is missing or isn't a key file's text; not using it"
  fi
  rm -f /tmp/kalshi_key.pem
fi
MODE=$(curl -fs -H "Metadata-Flavor: Google" "$META/btcbot-mode" || true)
if [ "$MODE" = live ] || [ "$MODE" = paper ]; then
  (cd "$REPO" && as_bot "$REPO/.venv/bin/python" - "$SETTINGS" "$MODE" <<'PY'
import sys
from btcbot.settings import LIVE_CONFIRM_TEXT, SettingsError, load_raw, save_raw
path, mode = sys.argv[1], sys.argv[2]
raw = load_raw(path)
if raw.get("mode") != mode:
    raw["mode"] = mode
    if mode == "live":
        raw["live_confirm"] = LIVE_CONFIRM_TEXT
    try:
        save_raw(path, raw)
        print(f"BTCBOT SETUP: switched to {'REAL MONEY' if mode == 'live' else 'pretend money'}")
    except SettingsError as e:
        print(f"BTCBOT SETUP: did NOT switch to {mode}: {e}")
PY
  )
fi
cat > /etc/systemd/system/btcbot.service <<EOF
[Unit]
Description=Kalshi 15-minute bot
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_AS
WorkingDirectory=$REPO
EnvironmentFile=-/etc/btcbot.env
ExecStart=$REPO/.venv/bin/python -m btcbot run
Restart=always
RestartSec=10
# Also print the log to the serial console, so it can be read without SSH.
StandardOutput=journal+console
StandardError=journal+console

[Install]
WantedBy=multi-user.target
EOF
# The private website (status + settings). Its key is made once and kept.
if [ ! -s "$KEY_FILE" ]; then
  as_bot "$REPO/.venv/bin/python" -c "import secrets;print(secrets.token_urlsafe(18))" > "$KEY_FILE.new" \
    && install -o "$RUN_AS" -g "$RUN_AS" -m 600 "$KEY_FILE.new" "$KEY_FILE"
  rm -f "$KEY_FILE.new"
fi
cat > /etc/systemd/system/btcbot-web.service <<EOF
[Unit]
Description=Kalshi bot website
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_AS
WorkingDirectory=$REPO
Environment=BTCBOT_WEB_KEY_FILE=$KEY_FILE
ExecStart=$REPO/.venv/bin/python -m btcbot web --port 80
AmbientCapabilities=CAP_NET_BIND_SERVICE
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable btcbot btcbot-web >/dev/null 2>&1
systemctl restart btcbot btcbot-web

say "checking the connection to Kalshi (no orders)"
(cd "$REPO" && as_bot "$REPO/.venv/bin/python" -m btcbot check 2>&1) | sed 's/^/BTCBOT CHECK: /'
sleep 3
IP=$(curl -fs -H "Metadata-Flavor: Google" "$IP_URL")
if systemctl is-active --quiet btcbot-web && [ -n "$IP" ]; then
  echo "BTCBOT WEBSITE: http://$IP/?key=$(cat "$KEY_FILE")"
  say "open that link on your phone (keep it private). If it won't load, tick Allow HTTP traffic on the server."
else
  say "the website did not start; last lines:"
  journalctl -u btcbot-web -n 20 --no-pager
fi
if systemctl is-active --quiet btcbot; then
  say "DONE. The bot is running. Its log lines follow below as they happen."
else
  say "the bot did not start; last log lines:"
  journalctl -u btcbot -n 20 --no-pager
fi
