#!/bin/bash
set -euo pipefail

PROJECT_DIR=$(cd "$(dirname "$0")/.." && pwd)
PYTHON="$PROJECT_DIR/.venv/bin/python"
APP="$PROJECT_DIR/dist/USB Watchdog.app"
BUNDLED_ENGINE="$APP/Contents/Resources/usb_watchdog.sh"

[[ "$PROJECT_DIR" != "/" && -f "$PROJECT_DIR/setup.py" ]] || {
    echo "Error: refusing to build outside the USB Watchdog project." >&2
    exit 1
}
[[ -x "$PYTHON" ]] || {
    echo "Error: create .venv and install requirements-build.txt first." >&2
    exit 1
}

"$PYTHON" -c 'import py2app, rumps' || {
    echo "Error: install the pinned build dependencies first:" >&2
    echo "  .venv/bin/python -m pip install -r requirements-build.txt" >&2
    exit 1
}

# These are generated, ignored paths under the validated project directory.
/bin/rm -rf "$PROJECT_DIR/build" "$PROJECT_DIR/dist"
(
    cd "$PROJECT_DIR"
    "$PYTHON" setup.py py2app
)

[[ -d "$APP" && -f "$BUNDLED_ENGINE" ]] || {
    echo "Error: py2app did not produce the expected app bundle." >&2
    exit 1
}

# Finder/file-provider attributes make strict code-signature validation fail.
/usr/bin/xattr -cr "$APP"
/usr/bin/codesign --force --deep --sign - "$APP"
/usr/bin/codesign --verify --deep --strict --verbose=2 "$APP"
/usr/bin/plutil -lint "$APP/Contents/Info.plist"
/usr/bin/cmp "$PROJECT_DIR/usb_watchdog.sh" "$BUNDLED_ENGINE"

echo "Built and verified: $APP"
echo "Signature: ad-hoc local only (not Developer ID signed or notarized)."
