#!/bin/bash
set -euo pipefail

PROJECT_DIR=$(cd "$(dirname "$0")/.." && pwd)
SOURCE="$PROJECT_DIR/usb_watchdog_events.c"
OUTPUT_DIR="$PROJECT_DIR/build"
OUTPUT="$OUTPUT_DIR/usb_watchdog_event_monitor"

[[ "$PROJECT_DIR" != "/" && -f "$SOURCE" ]] || {
    echo "Error: refusing to build outside the USB Watchdog project." >&2
    exit 1
}
[[ -x /usr/bin/clang ]] || {
    echo "Error: Xcode Command Line Tools are required to build the event monitor." >&2
    exit 1
}

/bin/mkdir -p "$OUTPUT_DIR"
/usr/bin/clang \
    -std=c11 \
    -O2 \
    -Wall \
    -Wextra \
    -Werror \
    -framework CoreFoundation \
    -framework IOKit \
    "$SOURCE" \
    -o "$OUTPUT"

echo "Built: $OUTPUT"
