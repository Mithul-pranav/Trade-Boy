#!/usr/bin/env bash
# Installs Trade-Boy on a fresh Ubuntu server and runs the TECH STOCKS desk as a system service on the
# Alpaca PAPER account. Run from the repository folder:  bash deploy/setup_stocks.sh
# Safe to run again: it keeps the saved keys unless you choose to replace them.
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE=/etc/tradeboy-stocks.env
RUN_USER="$(id -un)"

echo "== Installing Python and git"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip git >/dev/null

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
  read -rsp "Alpaca PAPER API key ID: " key; echo
  read -rsp "Alpaca PAPER secret key: " secret; echo
  read -rp  "Contact for SEC EDGAR (e.g. 'Trade-Boy you@example.com'): " sec
  read -rsp "Anthropic API key (sk-ant-...): " claude; echo
  read -rsp "Telegram bot token: " telegram; echo
  read -rp  "Telegram chat ID [450514729]: " chat
  sudo install -m 600 -o root -g root /dev/null "$ENV_FILE"
  sudo tee "$ENV_FILE" >/dev/null <<KEYS
TRADEBOY_ALPACA_KEY_ID=$key
TRADEBOY_ALPACA_SECRET_KEY=$secret
TRADEBOY_SEC_USER_AGENT=$sec
TRADEBOY_ANTHROPIC_API_KEY=$claude
TRADEBOY_TELEGRAM_BOT_TOKEN=$telegram
TRADEBOY_TELEGRAM_CHAT_ID=${chat:-450514729}
KEYS
fi

echo "== Checking the Alpaca paper connection"
sudo bash -c "set -a; . '$ENV_FILE'; set +a; cd '$DIR' && exec runuser -u '$RUN_USER' -- '$DIR/.venv/bin/python' -m tradeboy stocks check"

echo "== Starting the stocks desk as a service (restarts on crashes and after reboots)"
sudo tee /etc/systemd/system/tradeboy-stocks.service >/dev/null <<UNIT
[Unit]
Description=Trade-Boy tech stocks desk (Alpaca paper account)
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$DIR
EnvironmentFile=$ENV_FILE
ExecStart=$DIR/.venv/bin/python -m tradeboy stocks run --paper
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now tradeboy-stocks
sleep 5
sudo systemctl --no-pager --lines=5 status tradeboy-stocks || true
echo
echo "Done. The stocks desk is running; Telegram should say 'Stocks desk started'."
echo "Watch it:  tail -f $DIR/data/stocks_desk.log      Stop it:  sudo systemctl stop tradeboy-stocks"
