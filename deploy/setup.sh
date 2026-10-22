#!/usr/bin/env bash
# Installs Trade-Boy on a fresh Ubuntu server and runs the desk 24/7 as a system service on the
# OANDA practice (demo) account. Run from the repository folder:  bash deploy/setup.sh
# Safe to run again: it keeps the saved keys unless you choose to replace them.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE=/etc/tradeboy.env
RUN_USER="$(id -un)"

echo "== Installing Python and git"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip git >/dev/null

# Small free servers have 1 GB of memory; a swap file stops installs from running out of it.
if ! swapon --show | grep -q /swapfile; then
  echo "== Adding a 1 GB swap file"
  sudo fallocate -l 1G /swapfile && sudo chmod 600 /swapfile && sudo mkswap -q /swapfile && sudo swapon /swapfile
  grep -q /swapfile /etc/fstab || echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab >/dev/null
fi

echo "== Installing Trade-Boy"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install -q --upgrade pip
"$DIR/.venv/bin/pip" install -q -e "$DIR"

ask_keys=yes
if sudo test -f "$ENV_FILE"; then
  read -rp "Keys are already saved on this server. Replace them? [y/N] " answer
  [[ "$answer" =~ ^[Yy] ]] || ask_keys=no
fi
if [ "$ask_keys" = yes ]; then
  echo "== Your keys (typing is hidden; they are saved only on this server, in $ENV_FILE)"
  read -rsp "OANDA demo API token: " oanda; echo
  read -rp  "OANDA demo account ID [101-003-40783045-001]: " account
  read -rsp "Anthropic API key (sk-ant-...): " claude; echo
  read -rsp "Telegram bot token: " telegram; echo
  read -rp  "Telegram chat ID [450514729]: " chat
  sudo install -m 600 -o root -g root /dev/null "$ENV_FILE"
  sudo tee "$ENV_FILE" >/dev/null <<KEYS
OANDA_ENV=practice
OANDA_API_TOKEN=$oanda
OANDA_ACCOUNT_ID=${account:-101-003-40783045-001}
TRADEBOY_ANTHROPIC_API_KEY=$claude
TRADEBOY_TELEGRAM_BOT_TOKEN=$telegram
TRADEBOY_TELEGRAM_CHAT_ID=${chat:-450514729}
KEYS
fi

echo "== Checking the OANDA connection"
# The keys file is readable by root only, so root loads it and runs the check as you.
sudo bash -c "set -a; . '$ENV_FILE'; set +a; cd '$DIR' && exec runuser -u '$RUN_USER' -- '$DIR/.venv/bin/python' -m tradeboy check"

echo "== Starting the desk as a service (restarts on crashes and after reboots)"
sudo tee /etc/systemd/system/tradeboy.service >/dev/null <<UNIT
[Unit]
Description=Trade-Boy forex desk (OANDA practice account)
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$DIR
EnvironmentFile=$ENV_FILE
ExecStart=$DIR/.venv/bin/python -m tradeboy run --paper
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now tradeboy
sleep 5
sudo systemctl --no-pager --lines=5 status tradeboy || true
echo
echo "Done. The desk is running; Telegram should say 'Desk started'."
echo "Watch it:  tail -f $DIR/data/desk.log      Stop it:  sudo systemctl stop tradeboy"
