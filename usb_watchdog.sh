#!/bin/bash
# ========================================================================
#  usb_watchdog.sh — USB/Thunderbolt/SD Dead Man's Switch for macOS
# ========================================================================
#
#  Monitors all USB, Thunderbolt, and SD card ports for device additions
#  or removals. Devices are fingerprinted per physical port (USB locationID,
#  Thunderbolt UID) plus vendor/product/serial, so swapping a device on a
#  port — or substituting a same-named clone — is detected, not just plain
#  add/remove. If any change is detected, immediately forces a hard shutdown.
#  Works while locked, while the screen is off, and on lid-open from sleep.
#
#  Also monitors: HDMI / external displays (any non-internal display).
#  Does NOT monitor MagSafe (power only) or the 3.5mm audio jack (analog).
#
# ---- SETUP (one-time) -------------------------------------------------
#
#  1. cd into saved folder
#
#  2. make executable:
#       chmod +x usb_watchdog.sh
#
#  3. Test in dry-run mode first (no root needed):
#       ./usb_watchdog.sh --dry-run
#     Then plug in or remove a USB device to confirm detection works.
#
#  4. Arm for real (requires root for shutdown):
#       sudo ./usb_watchdog.sh
#
#  5. Run in background (persists after closing Terminal):
#       sudo nohup ./usb_watchdog.sh &
#     Close the terminal. Confirm it's running:
#       pgrep -fl usb_watchdog
#
#  6. To stop:
#       sudo pkill -f usb_watchdog
#
# ---- OPTIONS -----------------------------------------------------------
#
#   --dry-run    Test mode. Prints what would happen, no shutdown.
#   --help       Show this help.
#
#   Detection is INSTANT: any device change triggers an immediate hard
#   shutdown. There is no delay and no way to cancel once triggered.
#
# ---- AUTO-START AT LOGIN (optional) ------------------------------------
#
#   Create a plist to launch at boot:
#
#   sudo tee /Library/LaunchDaemons/com.user.usbwatchdog.plist > /dev/null << 'PLIST'
#   <?xml version="1.0" encoding="UTF-8"?>
#   <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
#     "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
#   <plist version="1.0">
#   <dict>
#     <key>Label</key>
#     <string>com.user.usbwatchdog</string>
#     <key>ProgramArguments</key>
#     <array>
#       <string>/Users/YOUR_USERNAME/Documents/scripts/usb_watchdog.sh</string>
#     </array>
#     <key>RunAtLoad</key>
#     <true/>
#     <key>KeepAlive</key>
#     <true/>
#     <key>StandardOutPath</key>
#     <string>/var/log/usb_watchdog.log</string>
#     <key>StandardErrorPath</key>
#     <string>/var/log/usb_watchdog.log</string>
#   </dict>
#   </plist>
#   PLIST
#
#   Replace YOUR_USERNAME and script path with your actual values, then:
#     sudo launchctl load /Library/LaunchDaemons/com.user.usbwatchdog.plist
#
#   To disable auto-start:
#     sudo launchctl unload /Library/LaunchDaemons/com.user.usbwatchdog.plist
#     sudo rm /Library/LaunchDaemons/com.user.usbwatchdog.plist
#
# ========================================================================

set -euo pipefail

# --- Configuration ---
# Tuned against measured probe costs (ioreg ~17ms, system_profiler ~190ms):
# detection stays ~0.3s for USB/TB and ~3s for SD/displays — both well inside
# any physical attack's timeline (OS device enumeration alone takes ~1s) —
# at roughly 15% of a core instead of the ~50% a 0.05s cadence costs.
FAST_INTERVAL=0.25   # USB/Thunderbolt poll cadence — high-risk ports (DMA/BadUSB)
SLOW_CYCLES=24       # re-check SD + displays every Nth fast cycle (~3s period)
DRY_RUN=false
SNAPSHOT_ONLY=false

# --- Parse arguments ---
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --snapshot)
            # Print the current device snapshot and exit (no root needed).
            # Used by the GUI to display the live device list.
            SNAPSHOT_ONLY=true
            shift
            ;;
        --help|-h)
            # Print the whole header comment block. BSD-safe: head has no
            # negative counts and BSD sed has no \? — and an end-pattern of
            # /^# ====/ would stop at the title banner's own border.
            awk 'NR==1 {next} /^#/ {sub(/^# ?/, ""); print; next} {exit}' "$0"
            exit 0
            ;;
        *)
            echo "Unknown option: $1 (try --help)"
            exit 1
            ;;
    esac
done

# --- Require root for real shutdown ---
if [[ "$DRY_RUN" == false && "$SNAPSHOT_ONLY" == false && $EUID -ne 0 ]]; then
    echo "Error: must run as root (sudo) for shutdown capability."
    echo "Use --dry-run to test without root."
    exit 1
fi

# --- Device snapshots ---
# Split by risk + cost: USB/Thunderbolt are the high-risk ports (DMA, BadUSB)
# and cheap to read via ioreg (~0.03s), so they're polled on a tight loop.
# SD cards and external displays are lower-risk and slower to read
# (system_profiler ~0.2s), so they're polled less frequently.

# Fast: USB + Thunderbolt (polled every cycle).
get_fast_snapshot() {
    {
        # USB: fingerprint each real peripheral by physical port (locationID)
        # plus vendor:product and serial number. This is per-port: swapping a
        # device on a port, or substituting a same-named clone, changes the
        # fingerprint and trips the switch.
        ioreg -p IOUSB -w0 -l 2>/dev/null | awk '
            function flush() {
                if (name != "" && vid != "") {
                    pn = (prod != "" ? prod : name)
                    print "USB:port=" loc " " vid ":" pid " sn=" sn " " pn
                }
            }
            /\+-o / {
                flush()
                name = $0
                sub(/.*\+-o /, "", name); sub(/@.*/, "", name); sub(/  <class.*/, "", name)
                gsub(/^[ \t]+|[ \t]+$/, "", name)
                vid=""; pid=""; loc=""; sn=""; prod=""
            }
            /"idVendor" = /  { vid = $NF }
            /"idProduct" = / { pid = $NF }
            /"locationID" = / { loc = $NF }
            /"USB Serial Number" = / { sn = $0; sub(/.*"USB Serial Number" = /, "", sn); gsub(/"/, "", sn) }
            /"USB Product Name" = / { prod = $0; sub(/.*"USB Product Name" = /, "", prod); gsub(/"/, "", prod) }
            END { flush() }
        '

        # Thunderbolt: fingerprint by UID (globally unique per device) plus
        # vendor and device name, so a swapped device is a different entry.
        ioreg -p IOThunderbolt -w0 -l 2>/dev/null | awk '
            function flush() {
                if (name != "" && vendor != "") {
                    dn = (devname != "" ? devname : name)
                    print "TB:uid=" uid " " vendor " / " dn
                }
            }
            /\+-o / {
                flush()
                name = $0
                sub(/.*\+-o /, "", name); sub(/@.*/, "", name); sub(/  <class.*/, "", name)
                gsub(/^[ \t]+|[ \t]+$/, "", name)
                vendor=""; devname=""; uid=""
            }
            /"Vendor Name" = / { vendor = $0; sub(/.*"Vendor Name" = /, "", vendor); gsub(/"/, "", vendor) }
            /"Device Name" = / { devname = $0; sub(/.*"Device Name" = /, "", devname); gsub(/"/, "", devname) }
            /"UID" = /         { uid = $0; sub(/.*"UID" = /, "", uid); gsub(/"/, "", uid) }
            END { flush() }
        '
    } | sort -u
}

# Slow: SD cards + external displays (polled every SLOW_CYCLES cycles).
get_slow_snapshot() {
    {
        # SD cards: detect inserted cards (not the empty reader itself)
        system_profiler SPCardReaderDataType 2>/dev/null | awk '
            /Card Reader/ { in_reader = 1; next }
            in_reader && /^        [A-Za-z]/ && !/Vendor ID|Device ID|Subsystem|Revision|Link Width|Link Speed/ {
                name = $0
                gsub(/^[ \t]+|:[ \t]*$/, "", name)
                if (name != "" && name !~ /Built in SD Card Reader/ && name !~ /^$/) print "SD:" name
            }
        '

        # HDMI / external displays: every display whose connection is not
        # internal (HDMI, DisplayPort, USB-C, etc.). Internal panel skipped.
        system_profiler SPDisplaysDataType 2>/dev/null | awk '
            /^        [A-Za-z].*:$/ {
                if (name != "" && !internal) print "DISPLAY:" name
                name = $0
                gsub(/^[ \t]+|:[ \t]*$/, "", name)
                internal = 0
            }
            /Connection Type:[ \t]*Internal/ { internal = 1 }
            /Display Type:.*Built-[Ii]n/     { internal = 1 }
            END { if (name != "" && !internal) print "DISPLAY:" name }
        '
    } | sort -u
}

# Combined — used for --snapshot output and the startup baseline display.
get_device_snapshot() {
    { get_fast_snapshot; get_slow_snapshot; } | sort -u
}

# --- Snapshot mode: print devices and exit (consumed by the GUI) ---
if [[ "$SNAPSHOT_ONLY" == true ]]; then
    get_device_snapshot
    exit 0
fi

# --- Shutdown function ---
# Instant: any detected change shuts down immediately. No delay, no cancel.
do_shutdown() {
    local reason="$1"

    if [[ "$DRY_RUN" == true ]]; then
        echo ""
        echo "$(date '+%H:%M:%S') !!! DRY RUN — would shut down now !!!"
        echo "  Reason: $reason"
        echo "  Resuming monitoring with updated baseline..."
        return 1
    fi

    echo "!!! DEVICE CHANGE DETECTED: $reason"
    echo "!!! EXECUTING SHUTDOWN NOW !!!"
    # Absolute paths so the shutdown never depends on PATH. -q = quick halt:
    # no app prompts, no sync, kernel-level, cannot be blocked or cancelled.
    # Never return in real mode: if this function fell through, the caller's
    # dry-run branch would adopt the changed devices as the new baseline —
    # the worst response to a failed shutdown. Retry forever; shutdown(8)
    # is the fallback since it powers off through a different code path.
    while true; do
        /sbin/halt -q
        /sbin/shutdown -h now
        sleep 1
    done
}

# Build a human-readable reason from a baseline/current diff, then act. In
# real mode do_shutdown halts and never returns; in dry-run it returns 1 so
# the caller updates its baseline and keeps watching.
process_change() {
    local base="$1" cur="$2" added removed reason=""
    removed=$(comm -23 <(echo "$base") <(echo "$cur") 2>/dev/null || true)
    added=$(comm -13 <(echo "$base") <(echo "$cur") 2>/dev/null || true)
    if [[ -n "$removed" ]]; then
        reason="REMOVED: $(echo "$removed" | tr '\n' ',' | sed 's/,$//')"
    fi
    if [[ -n "$added" ]]; then
        [[ -n "$reason" ]] && reason="$reason | "
        reason="${reason}ADDED: $(echo "$added" | tr '\n' ',' | sed 's/,$//')"
    fi
    do_shutdown "$reason"
}

# --- Cleanup (terminal Ctrl+C only) ---
cleanup() {
    echo ""
    echo "$(date '+%H:%M:%S') USB watchdog stopped."
    exit 0
}
trap cleanup SIGINT SIGTERM

# --- Main ---
echo "========================================"
echo "  USB Watchdog — Dead Man's Switch"
echo "========================================"
echo ""

# All snapshot reads are guarded with || true: under set -e, a transient
# ioreg/system_profiler failure inside the pipeline would otherwise kill
# the watchdog silently — fail-open for a security tool.
BASELINE=$(get_device_snapshot || true)
if [[ -z "$BASELINE" ]]; then
    DEVICE_COUNT=0
else
    DEVICE_COUNT=$(echo "$BASELINE" | wc -l | tr -d ' ')
fi

echo "Baseline devices ($DEVICE_COUNT):"
if [[ -n "$BASELINE" ]]; then
    echo "$BASELINE" | sed 's/^/  • /'
else
    echo "  (none)"
fi
echo ""
echo "Mode:          INSTANT (no delay, no cancel)"
echo "USB/TB poll:   every ${FAST_INTERVAL}s   (SD/display every ${SLOW_CYCLES} cycles)"
echo "Dry run:       $DRY_RUN"
echo "Monitoring:    USB, Thunderbolt, SD, HDMI/displays"
echo ""
echo "$(date '+%H:%M:%S') Armed. Watching for device changes..."
if [[ "$DRY_RUN" == false ]]; then
    echo "Press Ctrl+C to stop."
fi
echo ""

FAST_BASE=$(get_fast_snapshot || true)
SLOW_BASE=$(get_slow_snapshot || true)
cycle=0
last_cycle=$SECONDS

while true; do
    sleep "$FAST_INTERVAL"

    # Wake settle: a wall-clock jump between cycles means the machine was
    # asleep. Give the USB stack a moment to re-enumerate before comparing,
    # so a slow-waking hub doesn't read as a removal on lid-open. A device
    # genuinely changed during sleep still differs after the settle.
    if (( SECONDS - last_cycle > 2 )); then
        sleep 2
    fi
    last_cycle=$SECONDS

    # High-risk ports (USB/Thunderbolt) — checked every cycle. A suspected
    # change is confirmed with a second read before acting: a transient
    # ioreg glitch clears; a real device change persists.
    FAST_CUR=$(get_fast_snapshot || true)
    if [[ "$FAST_CUR" != "$FAST_BASE" ]]; then
        FAST_CUR=$(get_fast_snapshot || true)
    fi
    if [[ "$FAST_CUR" != "$FAST_BASE" ]]; then
        if process_change "$FAST_BASE" "$FAST_CUR"; then
            :  # real mode halted; unreachable
        else
            FAST_BASE="$FAST_CUR"   # dry-run — update baseline and keep watching
            echo "$(date '+%H:%M:%S') Baseline updated. Watching..."
            echo ""
        fi
    fi

    # Lower-risk ports (SD/displays) — checked every SLOW_CYCLES cycles,
    # with the same confirm-before-acting second read.
    cycle=$((cycle + 1))
    if (( cycle >= SLOW_CYCLES )); then
        cycle=0
        SLOW_CUR=$(get_slow_snapshot || true)
        if [[ "$SLOW_CUR" != "$SLOW_BASE" ]]; then
            SLOW_CUR=$(get_slow_snapshot || true)
        fi
        if [[ "$SLOW_CUR" != "$SLOW_BASE" ]]; then
            if process_change "$SLOW_BASE" "$SLOW_CUR"; then
                :
            else
                SLOW_BASE="$SLOW_CUR"
                echo "$(date '+%H:%M:%S') Baseline updated. Watching..."
                echo ""
            fi
        fi
    fi
done
