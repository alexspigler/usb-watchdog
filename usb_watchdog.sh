#!/bin/bash
# ========================================================================
#  usb_watchdog.sh — USB/Thunderbolt/SD/display change monitor for macOS
# ========================================================================
#
#  The monitor records observable hardware inventory fields and begins an
#  immediate OS shutdown when those fields change. It is a tamper alarm, not
#  cryptographic device authentication: cloned descriptors, changes completed
#  entirely between polls, and changes made and restored while the Mac sleeps
#  cannot be distinguished from an unchanged device set.
#
#  Normal use:
#      ./usb_watchdog.sh --dry-run
#      sudo ./usb_watchdog.sh
#
#  The menu-bar app launches an explicitly authorized background instance and
#  controls it through a permission-safe state record and per-launch token.
#  Direct command-line use remains independent of the menu-bar controller.
#
#  Options:
#      --dry-run                 Report changes without shutting down.
#      --snapshot                Print one validated device snapshot and exit.
#      --help                    Show this help.
#
#  Internal service options:
#      --state-file PATH
#      --instance-token TOKEN
#      --stop
#
# ========================================================================

# Polling and failure-policy configuration. The slow cadence is approximately
# 3 seconds: 0.25 seconds * 12 cycles, plus actual probe time.
FAST_INTERVAL=0.25
SLOW_CYCLES=12
PROBE_TIMEOUT_SECONDS=3
PROBE_RETRIES=2
STABLE_ATTEMPTS=4
WAKE_GAP_SECONDS=2
WAKE_SETTLE_SECONDS=2
GRACEFUL_SHUTDOWN_SECONDS=5

DRY_RUN=false
SNAPSHOT_ONLY=false
STOP_ONLY=false
STATE_FILE=""
INSTANCE_TOKEN=""
INSTANCE_STARTED=""
STATE_LOCK_DIR=""
STATE_LOCKED=false
LAST_HEARTBEAT=0
FAST_HEALTHY=false
SLOW_HEALTHY=false

usage() {
    /usr/bin/awk 'NR==1 {next} /^#/ {sub(/^# ?/, ""); print; next} {exit}' "$0"
}

fail() {
    echo "Error: $*" >&2
    return 1
}

parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dry-run)
                DRY_RUN=true
                shift
                ;;
            --snapshot)
                SNAPSHOT_ONLY=true
                shift
                ;;
            --state-file)
                [[ $# -ge 2 ]] || fail "--state-file requires an absolute path"
                STATE_FILE="$2"
                shift 2
                ;;
            --instance-token)
                [[ $# -ge 2 ]] || fail "--instance-token requires a token"
                INSTANCE_TOKEN="$2"
                shift 2
                ;;
            --stop)
                STOP_ONLY=true
                shift
                ;;
            --help|-h)
                usage
                exit 0
                ;;
            *)
                fail "unknown option: $1 (try --help)"
                ;;
        esac
    done

    if [[ -n "$STATE_FILE" ]]; then
        [[ "$STATE_FILE" == /* && "$STATE_FILE" != *$'\n'* ]] ||
            fail "--state-file must be a newline-free absolute path"
        [[ "$INSTANCE_TOKEN" =~ ^[A-Fa-f0-9-]{16,64}$ ]] ||
            fail "a 16-64 character hexadecimal instance token is required with --state-file"
    elif [[ -n "$INSTANCE_TOKEN" ]]; then
        fail "--instance-token requires --state-file"
    fi

    if [[ "$STOP_ONLY" == true ]]; then
        [[ -n "$STATE_FILE" && -n "$INSTANCE_TOKEN" ]] ||
            fail "--stop requires --state-file and --instance-token"
    fi
}

# macOS does not ship timeout(1). POSIX alarms survive exec, so the system Perl
# wrapper places a hard bound around each inventory command without a helper
# timer process or PID-reuse race.
run_with_timeout() {
    local seconds="$1"
    shift
    /usr/bin/perl -e '$seconds = shift @ARGV; alarm $seconds; exec @ARGV or exit 127' \
        "$seconds" "$@"
}

parse_usb_snapshot() {
    /usr/bin/awk '
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
        /"USB Serial Number" = / {
            sn = $0; sub(/.*"USB Serial Number" = /, "", sn); gsub(/"/, "", sn)
        }
        /"USB Product Name" = / {
            prod = $0; sub(/.*"USB Product Name" = /, "", prod); gsub(/"/, "", prod)
        }
        END { flush() }
    '
}

parse_thunderbolt_snapshot() {
    /usr/bin/awk '
        function flush() {
            if (name != "" && vendor != "") {
                dn = (devname != "" ? devname : name)
                observed_uid = (uid != "" ? uid : "unknown")
                print "TB:uid=" observed_uid " " vendor " / " dn
            }
        }
        /\+-o / {
            flush()
            name = $0
            sub(/.*\+-o /, "", name); sub(/@.*/, "", name); sub(/  <class.*/, "", name)
            gsub(/^[ \t]+|[ \t]+$/, "", name)
            vendor=""; devname=""; uid=""
        }
        /"Vendor Name" = / {
            vendor = $0; sub(/.*"Vendor Name" = /, "", vendor); gsub(/"/, "", vendor)
        }
        /"Device Name" = / {
            devname = $0; sub(/.*"Device Name" = /, "", devname); gsub(/"/, "", devname)
        }
        /"UID" = / {
            uid = $0; sub(/.*"UID" = /, "", uid); gsub(/"/, "", uid)
        }
        END { flush() }
    '
}

parse_sd_snapshot() {
    /usr/bin/awk '
        /Card Reader/ { in_reader = 1; next }
        in_reader && /^        [A-Za-z]/ &&
            !/Vendor ID|Device ID|Subsystem|Revision|Link Width|Link Speed/ {
            name = $0
            gsub(/^[ \t]+|:[ \t]*$/, "", name)
            if (name != "" && name !~ /Built in SD Card Reader/) print "SD:" name
        }
    '
}

parse_display_snapshot() {
    /usr/bin/awk '
        function flush() {
            if (name != "" && !internal) {
                observed_serial = (serial != "" ? " serial=" serial : "")
                print "DISPLAY:" name observed_serial
            }
        }
        /^        [A-Za-z].*:$/ {
            flush()
            name = $0
            gsub(/^[ \t]+|:[ \t]*$/, "", name)
            internal = 0; serial = ""
        }
        /Connection Type:[ \t]*Internal/ { internal = 1 }
        /Display Type:.*Built-[Ii]n/     { internal = 1 }
        /Display Serial Number:/ {
            serial = $0; sub(/.*Display Serial Number:[ \t]*/, "", serial)
        }
        END { flush() }
    '
}

get_usb_snapshot() {
    local raw
    if ! raw=$(run_with_timeout "$PROBE_TIMEOUT_SECONDS" \
        /usr/sbin/ioreg -p IOUSB -w0 -l 2>/dev/null); then
        return 1
    fi
    printf '%s\n' "$raw" | parse_usb_snapshot
}

get_thunderbolt_snapshot() {
    local raw
    if ! raw=$(run_with_timeout "$PROBE_TIMEOUT_SECONDS" \
        /usr/sbin/ioreg -p IOThunderbolt -w0 -l 2>/dev/null); then
        return 1
    fi
    printf '%s\n' "$raw" | parse_thunderbolt_snapshot
}

get_sd_snapshot() {
    local raw
    if ! raw=$(run_with_timeout "$PROBE_TIMEOUT_SECONDS" \
        /usr/sbin/system_profiler SPCardReaderDataType 2>/dev/null); then
        return 1
    fi
    printf '%s\n' "$raw" | parse_sd_snapshot
}

get_display_snapshot() {
    local raw
    if ! raw=$(run_with_timeout "$PROBE_TIMEOUT_SECONDS" \
        /usr/sbin/system_profiler SPDisplaysDataType 2>/dev/null); then
        return 1
    fi
    printf '%s\n' "$raw" | parse_display_snapshot
}

merge_snapshots() {
    local first="$1" second="$2"
    {
        if [[ -n "$first" ]]; then printf '%s\n' "$first"; fi
        if [[ -n "$second" ]]; then printf '%s\n' "$second"; fi
        true
    } | /usr/bin/sort -u
}

get_fast_snapshot() {
    local usb thunderbolt
    usb=$(get_usb_snapshot) || return 1
    thunderbolt=$(get_thunderbolt_snapshot) || return 1
    merge_snapshots "$usb" "$thunderbolt"
}

get_slow_snapshot() {
    local sd displays
    sd=$(get_sd_snapshot) || return 1
    displays=$(get_display_snapshot) || return 1
    merge_snapshots "$sd" "$displays"
}

get_device_snapshot() {
    local fast slow
    fast=$(get_fast_snapshot) || return 1
    slow=$(get_slow_snapshot) || return 1
    merge_snapshots "$fast" "$slow"
}

snapshot_getter() {
    case "$1" in
        fast) get_fast_snapshot ;;
        slow) get_slow_snapshot ;;
        all)  get_device_snapshot ;;
        *) return 2 ;;
    esac
}

read_snapshot_with_retries() {
    local kind="$1" attempt output
    attempt=1
    while (( attempt <= PROBE_RETRIES )); do
        if output=$(snapshot_getter "$kind"); then
            printf '%s' "$output"
            return 0
        fi
        /bin/sleep 0.2
        attempt=$((attempt + 1))
    done
    return 1
}

collect_stable_snapshot() {
    local kind="$1" attempt first second
    attempt=1
    while (( attempt <= STABLE_ATTEMPTS )); do
        stop_requested && return 130
        if first=$(snapshot_getter "$kind"); then
            stop_requested && return 130
            /bin/sleep 0.15
            if second=$(snapshot_getter "$kind") && [[ "$first" == "$second" ]]; then
                printf '%s' "$second"
                return 0
            fi
            stop_requested && return 130
        fi
        /bin/sleep 0.25
        attempt=$((attempt + 1))
    done
    return 1
}

state_value() {
    local key="$1" file="$2"
    [[ -f "$file" && ! -L "$file" ]] || return 1
    /usr/bin/awk -F= -v wanted="$key" '$1 == wanted {sub(/^[^=]*=/, ""); print; exit}' "$file"
}

process_matches_instance() {
    local pid="$1" token="$2" expected_started="${3:-}" expected_uid="${4:-}"
    local command current_started current_uid
    [[ "$pid" =~ ^[0-9]+$ && "$pid" -gt 1 ]] || return 1
    command=$(TZ=UTC LC_ALL=C /bin/ps -ww -p "$pid" -o command= 2>/dev/null) || return 1
    [[ "$command" == *"usb_watchdog.sh"* &&
       "$command" == *"--instance-token $token"* ]] || return 1
    if [[ -n "$expected_started" ]]; then
        current_started=$(TZ=UTC LC_ALL=C /bin/ps -p "$pid" -o lstart= 2>/dev/null) || return 1
        current_started=$(printf '%s' "$current_started" |
            /usr/bin/sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
        [[ "$current_started" == "$expected_started" ]] || return 1
    fi
    if [[ -n "$expected_uid" ]]; then
        current_uid=$(TZ=UTC LC_ALL=C /bin/ps -p "$pid" -o uid= 2>/dev/null) || return 1
        current_uid=$(printf '%s' "$current_uid" | /usr/bin/tr -d ' ')
        [[ "$current_uid" == "$expected_uid" ]] || return 1
    fi
}

prepare_state() {
    local state_dir state_owner existing_pid existing_token existing_started existing_uid
    [[ -n "$STATE_FILE" ]] || return 0

    state_dir=$(/usr/bin/dirname "$STATE_FILE")
    if [[ ! -d "$state_dir" ]]; then
        /bin/mkdir -p "$state_dir"
        if [[ $EUID -eq 0 ]]; then
            /bin/chmod 0755 "$state_dir"
        else
            /bin/chmod 0700 "$state_dir"
        fi
    elif [[ $EUID -ne 0 ]]; then
        [[ ! -L "$state_dir" ]] || fail "state directory must not be a symlink"
        state_owner=$(/usr/bin/stat -f '%u' "$state_dir") ||
            fail "cannot inspect state directory ownership"
        [[ "$state_owner" == "$EUID" ]] ||
            fail "state directory must be owned by the current user"
        /bin/chmod 0700 "$state_dir"
    fi

    STATE_LOCK_DIR="${STATE_FILE}.lock"
    if ! /bin/mkdir "$STATE_LOCK_DIR" 2>/dev/null; then
        existing_pid=$(state_value pid "$STATE_FILE" 2>/dev/null || true)
        existing_token=$(state_value token "$STATE_FILE" 2>/dev/null || true)
        existing_started=$(state_value started "$STATE_FILE" 2>/dev/null || true)
        existing_uid=$(state_value uid "$STATE_FILE" 2>/dev/null || true)
        if [[ -n "$existing_pid" && -n "$existing_token" ]] &&
            process_matches_instance "$existing_pid" "$existing_token" "$existing_started" "$existing_uid"; then
            fail "another watchdog instance is already registered at $STATE_FILE"
        fi
        /bin/rmdir "$STATE_LOCK_DIR" 2>/dev/null ||
            fail "cannot recover stale watchdog lock at $STATE_LOCK_DIR"
        /bin/mkdir "$STATE_LOCK_DIR" 2>/dev/null ||
            fail "another watchdog instance started concurrently"
    fi
    STATE_LOCKED=true
    /bin/rm -f "${STATE_FILE}.stop"
    write_state "starting" "initializing"
}

write_state() {
    local status="$1" detail="${2:-}" temp state_mode file_mode now
    [[ -n "$STATE_FILE" ]] || return 0
    detail=${detail//$'\n'/ }
    state_mode="real"
    [[ "$DRY_RUN" == true ]] && state_mode="dry-run"
    now=$(/bin/date +%s)
    temp="${STATE_FILE}.tmp.$$"
    file_mode=0600
    [[ $EUID -eq 0 ]] && file_mode=0644
    (
        umask 077
        {
            printf 'version=1\n'
            printf 'pid=%s\n' "$$"
            printf 'uid=%s\n' "$EUID"
            printf 'token=%s\n' "$INSTANCE_TOKEN"
            printf 'started=%s\n' "$INSTANCE_STARTED"
            printf 'mode=%s\n' "$state_mode"
            printf 'status=%s\n' "$status"
            printf 'heartbeat=%s\n' "$now"
            printf 'detail=%s\n' "$detail"
        } > "$temp"
    )
    /bin/chmod "$file_mode" "$temp"
    /bin/mv -f "$temp" "$STATE_FILE"
    LAST_HEARTBEAT="$now"
}

write_heartbeat() {
    local now
    [[ -n "$STATE_FILE" ]] || return 0
    now=$(/bin/date +%s)
    if [[ "$now" != "$LAST_HEARTBEAT" ]]; then
        if [[ "$FAST_HEALTHY" == true && "$SLOW_HEALTHY" == true ]]; then
            write_state "ready" "monitoring"
        else
            write_state "fault" "one or more probe groups await recovery"
        fi
    fi
}

stop_request_file() {
    printf '%s.stop' "$STATE_FILE"
}

stop_requested() {
    local request_file request_owner request_mode request_token request_started
    [[ -n "$STATE_FILE" && -n "$INSTANCE_TOKEN" && -n "$INSTANCE_STARTED" ]] || return 1
    request_file=$(stop_request_file)
    [[ -f "$request_file" && ! -L "$request_file" ]] || return 1
    request_owner=$(/usr/bin/stat -f '%u' "$request_file" 2>/dev/null) || return 1
    request_mode=$(/usr/bin/stat -f '%OLp' "$request_file" 2>/dev/null) || return 1
    [[ "$request_owner" == "$EUID" ]] || return 1
    (( (8#$request_mode & 0022) == 0 )) || return 1
    request_token=$(state_value token "$request_file" 2>/dev/null || true)
    request_started=$(state_value started "$request_file" 2>/dev/null || true)
    [[ "$request_token" == "$INSTANCE_TOKEN" &&
       "$request_started" == "$INSTANCE_STARTED" ]]
}

remove_owned_state() {
    local recorded_token
    [[ -n "$STATE_FILE" ]] || return 0
    recorded_token=$(state_value token "$STATE_FILE" 2>/dev/null || true)
    if [[ "$recorded_token" == "$INSTANCE_TOKEN" ]]; then
        /bin/rm -f "$STATE_FILE" "${STATE_FILE}.tmp.$$" "$(stop_request_file)"
    fi
    if [[ "$STATE_LOCKED" == true && -n "$STATE_LOCK_DIR" ]]; then
        /bin/rmdir "$STATE_LOCK_DIR" 2>/dev/null || true
    fi
}

stop_registered_instance() {
    local recorded_pid recorded_uid recorded_token recorded_started
    local request_file request_temp request_mode
    recorded_pid=$(state_value pid "$STATE_FILE") || fail "watchdog state is unavailable"
    recorded_uid=$(state_value uid "$STATE_FILE") || fail "watchdog state UID is unavailable"
    recorded_token=$(state_value token "$STATE_FILE") || fail "watchdog state token is unavailable"
    recorded_started=$(state_value started "$STATE_FILE") || fail "watchdog start identity is unavailable"
    [[ "$recorded_token" == "$INSTANCE_TOKEN" ]] || fail "watchdog instance token changed"
    if ! process_matches_instance "$recorded_pid" "$recorded_token" "$recorded_started" "$recorded_uid"; then
        # A matching, correctly owned record with a dead/reused PID is stale.
        # Remove only that exact record; never search for or signal a process.
        /bin/rm -f "$STATE_FILE" "$(stop_request_file)"
        /bin/rmdir "${STATE_FILE}.lock" 2>/dev/null || true
        return 0
    fi

    request_file=$(stop_request_file)
    request_temp="${request_file}.tmp.$$"
    request_mode=0600
    [[ $EUID -eq 0 ]] && request_mode=0644
    (
        umask 077
        {
            printf 'token=%s\n' "$recorded_token"
            printf 'started=%s\n' "$recorded_started"
        } > "$request_temp"
    )
    /bin/chmod "$request_mode" "$request_temp"
    /bin/mv -f "$request_temp" "$request_file"

    wait_for_instance_stop ||
        fail "watchdog did not acknowledge the exact stop request"
}

wait_for_instance_stop() {
    local attempt=1 current_token
    while (( attempt <= 200 )); do
        current_token=$(state_value token "$STATE_FILE" 2>/dev/null || true)
        [[ "$current_token" != "$INSTANCE_TOKEN" ]] && return 0
        /bin/sleep 0.1
        attempt=$((attempt + 1))
    done
    return 1
}

format_snapshot() {
    local snapshot="$1"
    if [[ -n "$snapshot" ]]; then
        printf '%s\n' "$snapshot" | /usr/bin/sed 's/^/  • /'
    else
        echo "  (none)"
    fi
}

do_shutdown() {
    local reason="$1"
    write_state "shutting-down" "$reason" || true

    if [[ "$DRY_RUN" == true ]]; then
        echo ""
        echo "$(/bin/date '+%H:%M:%S') !!! DRY RUN — shutdown required !!!"
        echo "  Reason: $reason"
        return 1
    fi

    echo "!!! WATCHDOG TRIGGERED: $reason"
    echo "!!! STARTING SHUTDOWN NOW !!!"
    # Begin the normal, syncing shutdown immediately. If the system is still
    # running after the grace period, fall back to an ungraceful quick halt.
    /sbin/shutdown -h now || true
    /bin/sleep "$GRACEFUL_SHUTDOWN_SECONDS"
    while true; do
        /sbin/halt -q || true
        /bin/sleep 1
    done
}

process_change() {
    local base="$1" current="$2" removed added reason=""
    removed=$(/usr/bin/comm -23 \
        <(printf '%s\n' "$base" | /usr/bin/sed '/^$/d') \
        <(printf '%s\n' "$current" | /usr/bin/sed '/^$/d') 2>/dev/null || true)
    added=$(/usr/bin/comm -13 \
        <(printf '%s\n' "$base" | /usr/bin/sed '/^$/d') \
        <(printf '%s\n' "$current" | /usr/bin/sed '/^$/d') 2>/dev/null || true)
    if [[ -n "$removed" ]]; then
        reason="REMOVED: $(printf '%s\n' "$removed" | /usr/bin/tr '\n' ',' | /usr/bin/sed 's/,$//')"
    fi
    if [[ -n "$added" ]]; then
        [[ -n "$reason" ]] && reason="$reason | "
        reason="${reason}ADDED: $(printf '%s\n' "$added" | /usr/bin/tr '\n' ',' | /usr/bin/sed 's/,$//')"
    fi
    do_shutdown "$reason"
}

runtime_probe_fault() {
    local probe_group="$1"
    write_state "fault" "$probe_group probe failed or timed out"
    if [[ "$DRY_RUN" == true ]]; then
        echo "$(/bin/date '+%H:%M:%S') DRY RUN — $probe_group probe fault; retaining last known-good baseline."
        /bin/sleep 1
        return 1
    fi
    do_shutdown "PROBE FAULT: $probe_group inventory failed after bounded retries"
}

initialize_baselines() {
    write_state "starting" "establishing stable baseline"
    FAST_BASE=$(collect_stable_snapshot fast) || return $?
    SLOW_BASE=$(collect_stable_snapshot slow) || return $?
    FAST_HEALTHY=true
    SLOW_HEALTHY=true
}

signal_exit() {
    exit 0
}

monitor_loop() {
    local fast_current slow_current cycle last_cycle now
    cycle=0
    last_cycle=$SECONDS

    while true; do
        /bin/sleep "$FAST_INTERVAL"
        stop_requested && return 0
        now=$SECONDS

        if (( now - last_cycle > WAKE_GAP_SECONDS )); then
            write_state "settling" "system wake detected"
            /bin/sleep "$WAKE_SETTLE_SECONDS"
            FAST_HEALTHY=false
            SLOW_HEALTHY=false
            fast_current=$(collect_stable_snapshot fast) || {
                stop_requested && return 0
                runtime_probe_fault "post-wake USB/Thunderbolt" || true
                last_cycle=$SECONDS
                continue
            }
            FAST_HEALTHY=true
            slow_current=$(collect_stable_snapshot slow) || {
                stop_requested && return 0
                runtime_probe_fault "post-wake SD/display" || true
                last_cycle=$SECONDS
                continue
            }
            SLOW_HEALTHY=true
            if [[ "$fast_current" != "$FAST_BASE" ]]; then
                if ! process_change "$FAST_BASE" "$fast_current"; then
                    FAST_BASE="$fast_current"
                fi
            fi
            if [[ "$slow_current" != "$SLOW_BASE" ]]; then
                if ! process_change "$SLOW_BASE" "$slow_current"; then
                    SLOW_BASE="$slow_current"
                fi
            fi
            write_state "ready" "monitoring after wake"
            cycle=0
            last_cycle=$SECONDS
            continue
        fi

        if ! fast_current=$(get_fast_snapshot); then
            FAST_HEALTHY=false
            write_state "degraded" "USB/Thunderbolt probe retrying"
            fast_current=$(read_snapshot_with_retries fast) || {
                runtime_probe_fault "USB/Thunderbolt" || true
                last_cycle=$SECONDS
                continue
            }
        fi
        if [[ "$fast_current" != "$FAST_BASE" ]]; then
            fast_current=$(read_snapshot_with_retries fast) || {
                FAST_HEALTHY=false
                runtime_probe_fault "USB/Thunderbolt confirmation" || true
                last_cycle=$SECONDS
                continue
            }
            if [[ "$fast_current" != "$FAST_BASE" ]]; then
                if ! process_change "$FAST_BASE" "$fast_current"; then
                    FAST_BASE="$fast_current"
                    echo "$(/bin/date '+%H:%M:%S') Dry-run baseline updated."
                fi
            fi
        fi
        FAST_HEALTHY=true

        cycle=$((cycle + 1))
        if (( cycle >= SLOW_CYCLES )); then
            cycle=0
            if ! slow_current=$(get_slow_snapshot); then
                SLOW_HEALTHY=false
                write_state "degraded" "SD/display probe retrying"
                slow_current=$(read_snapshot_with_retries slow) || {
                    runtime_probe_fault "SD/display" || true
                    last_cycle=$SECONDS
                    continue
                }
            fi
            if [[ "$slow_current" != "$SLOW_BASE" ]]; then
                slow_current=$(read_snapshot_with_retries slow) || {
                    SLOW_HEALTHY=false
                    runtime_probe_fault "SD/display confirmation" || true
                    last_cycle=$SECONDS
                    continue
                }
                if [[ "$slow_current" != "$SLOW_BASE" ]]; then
                    if ! process_change "$SLOW_BASE" "$slow_current"; then
                        SLOW_BASE="$slow_current"
                        echo "$(/bin/date '+%H:%M:%S') Dry-run baseline updated."
                    fi
                fi
            fi
            SLOW_HEALTHY=true
        fi

        write_heartbeat
        last_cycle=$SECONDS
    done
}

main() {
    local combined count
    set -euo pipefail
    parse_args "$@"

    if [[ "$STOP_ONLY" == true ]]; then
        stop_registered_instance
        exit 0
    fi

    if [[ "$SNAPSHOT_ONLY" == true ]]; then
        if ! get_device_snapshot; then
            echo "Error: one or more hardware inventory probes failed or timed out." >&2
            exit 2
        fi
        exit 0
    fi

    if [[ "$DRY_RUN" == false && $EUID -ne 0 ]]; then
        fail "real mode must run as root; use --dry-run to test"
    fi

    if [[ -n "$STATE_FILE" ]]; then
        INSTANCE_STARTED=$(TZ=UTC LC_ALL=C /bin/ps -p $$ -o lstart= 2>/dev/null) ||
            fail "could not obtain watchdog process start identity"
        INSTANCE_STARTED=$(printf '%s' "$INSTANCE_STARTED" |
            /usr/bin/sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
    fi

    prepare_state
    trap remove_owned_state EXIT
    trap signal_exit SIGINT SIGTERM

    if ! initialize_baselines; then
        stop_requested && exit 0
        write_state "fault" "could not establish a complete stable baseline" || true
        echo "Error: could not establish a complete stable hardware baseline." >&2
        exit 2
    fi

    combined=$(merge_snapshots "$FAST_BASE" "$SLOW_BASE")
    if [[ -n "$combined" ]]; then count=$(printf '%s\n' "$combined" | /usr/bin/wc -l | /usr/bin/tr -d ' '); else count=0; fi

    echo "========================================"
    echo "  USB Watchdog — Hardware Change Monitor"
    echo "========================================"
    echo ""
    echo "Validated baseline devices ($count):"
    format_snapshot "$combined"
    echo ""
    echo "USB/TB poll:    approximately every ${FAST_INTERVAL}s"
    echo "SD/display:     approximately every $((SLOW_CYCLES)) fast cycles"
    echo "After wake:     compare a new stable snapshot"
    echo "Dry run:        $DRY_RUN"
    echo ""
    echo "$(/bin/date '+%H:%M:%S') Ready. Monitoring validated snapshots."

    write_state "ready" "monitoring"
    monitor_loop
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
