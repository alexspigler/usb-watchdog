#!/bin/bash
# Double-click this in Finder to launch the USB Watchdog menu bar app.
# Runs the GUI with the project's virtual environment (where rumps lives).
DIR="$(cd "$(dirname "$0")" && pwd)"
if [[ ! -x "$DIR/.venv/bin/python" ]]; then
    echo "USB Watchdog's Python environment is missing."
    echo "Open Terminal in this folder and follow the setup steps in README.md."
    read -r -p "Press Return to close..."
    exit 1
fi
exec "$DIR/.venv/bin/python" "$DIR/usb_watchdog_gui.py"
