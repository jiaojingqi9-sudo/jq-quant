#!/bin/zsh
# Launch the trading control panel (Tkinter GUI).
# Starts Python in the background then closes this Terminal window so only
# the control-panel window is visible — no extra Python/Terminal windows.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -x ".venv/bin/python" ]]; then
  osascript -e 'display alert "Missing .venv/bin/python" message "The local Python environment is missing. Ask Claude to reinstall the project dependencies." as critical'
  exit 1
fi

export PYTHONPATH="$SCRIPT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"

# uv-managed Python on macOS sometimes needs explicit Tcl/Tk library hints
# when launched from a detached Terminal session.
UV_LIB_ROOT="$HOME/.local/share/uv/python/cpython-3.11.14-macos-aarch64-none/lib"
if [[ -d "$UV_LIB_ROOT/tcl8.6" ]]; then
  export TCL_LIBRARY="$UV_LIB_ROOT/tcl8.6"
fi
if [[ -d "$UV_LIB_ROOT/tk8.6" ]]; then
  export TK_LIBRARY="$UV_LIB_ROOT/tk8.6"
fi

# Launch control panel detached from this terminal session.
# Logs go to /tmp/taa_control_panel.log for debugging.
nohup ".venv/bin/python" -m taa_futu.control_panel \
  > /tmp/taa_control_panel.log 2>&1 &
disown $!   # detach from shell job table so Terminal won't show "terminate" dialog

# Give Python a moment to start (detect early crash before closing terminal).
sleep 0.8
if ! pgrep -f "taa_futu.control_panel" > /dev/null 2>&1; then
  osascript -e 'display alert "启动失败 / Launch Failed" message "Control panel exited immediately. Check /tmp/taa_control_panel.log for details." as critical'
  exit 1
fi

# Close this Terminal window — only the Tkinter control panel remains.
osascript -e 'tell application "Terminal" to close front window'
