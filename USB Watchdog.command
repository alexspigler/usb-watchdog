#!/bin/bash
# Double-click this in Finder to launch the USB Watchdog menu bar app.
# Runs the GUI with the project's virtual environment (where rumps lives).
DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$DIR/.venv/bin/python" "$DIR/usb_watchdog_gui.py"
