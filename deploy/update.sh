#!/usr/bin/env bash
# Pulls the latest code from main and restarts the desk. Run from the repository folder: bash deploy/update.sh
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
git -C "$DIR" pull --ff-only origin main
"$DIR/.venv/bin/pip" install -q -e "$DIR"
sudo systemctl restart tradeboy
echo "Updated to $(git -C "$DIR" log --oneline -1) and restarted."
