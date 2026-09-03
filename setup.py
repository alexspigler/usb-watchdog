"""
py2app build script for the USB Watchdog menu bar app.

Build a standalone .app:
    .venv/bin/python setup.py py2app

The result lands in dist/USB Watchdog.app — self-contained (bundles its own
Python + rumps) and dock-less (LSUIElement), living only in the menu bar.
"""
from setuptools import setup

APP = ["usb_watchdog_gui.py"]
DATA_FILES = ["usb_watchdog.sh"]
VERSION = "1.2.0"
OPTIONS = {
    "argv_emulation": False,
    "packages": ["rumps"],
    "plist": {
        "CFBundleName": "USB Watchdog",
        "CFBundleDisplayName": "USB Watchdog",
        "CFBundleIdentifier": "com.alexspigler.usbwatchdog",
        "CFBundleVersion": VERSION,
        "CFBundleShortVersionString": VERSION,
        "LSUIElement": True,  # menu-bar-only; no Dock icon, no app menu
        "NSHumanReadableCopyright": "Copyright 2026 Alex Spigler",
    },
}

setup(
    app=APP,
    name="USB Watchdog",
    version=VERSION,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
)
