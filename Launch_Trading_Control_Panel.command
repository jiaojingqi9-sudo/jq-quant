#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -x ".venv/bin/python" ]]; then
  osascript -e 'display alert "Missing .venv/bin/python" message "The local Python environment is missing. Ask Codex to reinstall the project dependencies." as critical'
  exit 1
fi

export PYTHONPATH="$SCRIPT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
exec ".venv/bin/python" -m taa_futu.control_panel
