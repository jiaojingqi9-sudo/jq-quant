#!/bin/zsh
set -e

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT_DIR"

if [ -x "/opt/anaconda3/bin/python3" ]; then
  /opt/anaconda3/bin/python3 crypto/tools/crypto_data_downloader_desktop.py
elif command -v uv >/dev/null 2>&1; then
  uv run python crypto/tools/crypto_data_downloader_desktop.py
else
  "$HOME/.local/bin/uv" run python crypto/tools/crypto_data_downloader_desktop.py
fi
