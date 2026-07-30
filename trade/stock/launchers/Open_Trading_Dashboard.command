#!/bin/zsh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$PROJECT_DIR"

if [[ ! -x ".venv/bin/python" ]]; then
  osascript -e 'display alert "Missing .venv/bin/python" message "The local Python environment is missing. Ask Codex to reinstall the project dependencies." as critical'
  exit 1
fi

export PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
exec ".venv/bin/python" -m taa_futu.cli dashboard
