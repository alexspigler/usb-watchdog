"""Build the macOS menu-bar app with ./scripts/build_app.sh.

The bundle in dist/USB Watchdog.app includes Python, rumps, and the event listener.
"""
from setuptools import setup

APP = ["usb_watchdog_gui.py"]
DATA_FILES = ["usb_watchdog.sh"]
VERSION = "1.3.1"
OPTIONS = {
    "argv_emulation": False,
    "excludes": ["tkinter"],
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
