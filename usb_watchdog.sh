#!/bin/bash
# ========================================================================
#  usb_watchdog.sh — USB/Thunderbolt/SD/display change monitor for macOS
# ========================================================================
#
#  The monitor records observable hardware inventory fields and starts the
#  selected shutdown response when those fields change. It is a tamper alarm,
#  not cryptographic device authentication: cloned descriptors and changes made
#  and restored while the Mac sleeps can be indistinguishable from an unchanged
#  device set.
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
#      --shutdown-policy POLICY  graceful-then-force (default) or
#                                force-immediately.
#      --help                    Show this help.
#
#  Internal service options:
#      --state-file PATH
#      --instance-token TOKEN
#      --event-monitor-uid UID
#      --stop
#
# ========================================================================

# Polling and failure-policy configuration. USB events request an immediate
# inventory check when the native helper is available. The timed loop remains a
# fallback. The slow cadence includes 12 waits, 12 fast probes, and a slow probe.
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
FAST_INTERVAL=0.25
SLOW_CYCLES=12
PROBE_TIMEOUT_SECONDS=3
PROBE_RETRIES=2
STABLE_ATTEMPTS=4
WAKE_GAP_SECONDS=2
WAKE_SETTLE_SECONDS=2
GRACEFUL_SHUTDOWN_SECONDS=5
EVENT_START_TIMEOUT_SECONDS=2
EVENT_HEARTBEAT_TIMEOUT_SECONDS=3
EVENT_RETRY_SECONDS=5

DRY_RUN=false
SNAPSHOT_ONLY=false
STOP_ONLY=false
SHUTDOWN_POLICY="graceful-then-force"
STATE_FILE=""
INSTANCE_TOKEN=""
INSTANCE_STARTED=""
STATE_LOCK_DIR=""
STATE_LOCKED=false
LAST_HEARTBEAT=0
FAST_HEALTHY=false
SLOW_HEALTHY=false
EVENT_MONITOR_ACTIVE=false
EVENT_MONITOR_HEALTHY=false
EVENT_MONITOR_PID=""
EVENT_MONITOR_FD=9
EVENT_MONITOR_FD_OPEN=false
EVENT_LAST_HEARTBEAT=0
EVENT_LAST_START_ATTEMPT=0
EVENT_MONITOR_UID=""
EVENT_MONITOR_RETRY_ENABLED=false

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
            --shutdown-policy)
                [[ $# -ge 2 ]] || fail "--shutdown-policy requires a policy"
                SHUTDOWN_POLICY="$2"
                shift 2
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
            --event-monitor-uid)
                [[ $# -ge 2 ]] || fail "--event-monitor-uid requires a UID"
                EVENT_MONITOR_UID="$2"
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

    if [[ -n "$EVENT_MONITOR_UID" ]]; then
        [[ "$EVENT_MONITOR_UID" =~ ^[0-9]+$ && "$EVENT_MONITOR_UID" -gt 0 ]] ||
            fail "--event-monitor-uid must be a positive numeric UID"
        [[ $EUID -eq 0 ]] ||
            fail "--event-monitor-uid is only valid for a root monitor"
    fi

    if [[ "$STOP_ONLY" == true ]]; then
        [[ -n "$STATE_FILE" && -n "$INSTANCE_TOKEN" ]] ||
            fail "--stop requires --state-file and --instance-token"
    fi

    case "$SHUTDOWN_POLICY" in
        graceful-then-force|force-immediately) ;;
        *) fail "--shutdown-policy must be graceful-then-force or force-immediately" ;;
    esac
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

# Bash's process timer is not a reliable measure of time spent in system sleep
# on every macOS wake path. Epoch time advances while the machine is asleep.
wall_time() {
    /bin/date +%s
}

event_monitor_path() {
    local candidate
    for candidate in \
        "$SCRIPT_DIR/usb_watchdog_event_monitor" \
        "$SCRIPT_DIR/build/usb_watchdog_event_monitor"; do
        if [[ -f "$candidate" && ! -L "$candidate" && -x "$candidate" ]]; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    return 1
}

stop_usb_event_monitor() {
    local pid="$EVENT_MONITOR_PID" attempt
    if [[ "$EVENT_MONITOR_FD_OPEN" == true ]]; then
        exec 9<&-
        EVENT_MONITOR_FD_OPEN=false
    fi
    if [[ "$pid" =~ ^[0-9]+$ && "$pid" -gt 1 ]]; then
        /bin/kill "$pid" 2>/dev/null || true
        attempt=0
        while /bin/kill -0 "$pid" 2>/dev/null && (( attempt < 10 )); do
            /bin/sleep 0.05
            attempt=$((attempt + 1))
        done
        if /bin/kill -0 "$pid" 2>/dev/null; then
            /bin/kill -KILL "$pid" 2>/dev/null || true
        fi
        wait "$pid" 2>/dev/null || true
    fi
    EVENT_MONITOR_ACTIVE=false
    EVENT_MONITOR_HEALTHY=false
    EVENT_MONITOR_PID=""
}

start_usb_event_monitor() {
    local helper ready_message
    EVENT_LAST_START_ATTEMPT=$(wall_time) || return 1
    helper=$(event_monitor_path) || return 1

    if [[ $EUID -eq 0 ]]; then
        if [[ -z "$EVENT_MONITOR_UID" && "${SUDO_UID:-}" =~ ^[0-9]+$ &&
              "${SUDO_UID:-0}" -gt 0 ]]; then
            EVENT_MONITOR_UID="$SUDO_UID"
        fi
        [[ "$EVENT_MONITOR_UID" =~ ^[0-9]+$ && "$EVENT_MONITOR_UID" -gt 0 ]] ||
            return 1
        # sudo drops credentials before execve opens the mutable helper path,
        # so replacing the helper cannot turn it into root code execution.
        exec 9< <(/usr/bin/sudo -n -u "#$EVENT_MONITOR_UID" -- "$helper" --events)
    else
        exec 9< <("$helper" --events)
    fi
    EVENT_MONITOR_PID=$!
    EVENT_MONITOR_FD_OPEN=true
    if IFS= read -r -t "$EVENT_START_TIMEOUT_SECONDS" \
        -u "$EVENT_MONITOR_FD" ready_message && [[ "$ready_message" == "ready" ]]; then
        EVENT_MONITOR_ACTIVE=true
        EVENT_MONITOR_HEALTHY=true
        EVENT_LAST_HEARTBEAT=$SECONDS
        return 0
    fi

    stop_usb_event_monitor
    return 1
}

mark_event_monitor_unavailable() {
    local reason="${1:-unknown listener failure}"
    stop_usb_event_monitor
    EVENT_MONITOR_HEALTHY=false
    echo "$(/bin/date '+%H:%M:%S') USB event monitor unavailable ($reason); polling fallback remains active."
    if [[ "$FAST_HEALTHY" == true && "$SLOW_HEALTHY" == true ]]; then
        write_state "polling" "USB event monitor unavailable ($reason); polling fallback active" || true
    else
        write_state "fault" "event monitor unavailable ($reason) and an inventory probe is unhealthy" || true
    fi
}

restart_usb_event_monitor_after_wake() {
    stop_usb_event_monitor
    EVENT_MONITOR_HEALTHY=false
    if start_usb_event_monitor; then
        echo "$(/bin/date '+%H:%M:%S') USB event monitor restarted after wake."
        return 0
    fi

    echo "$(/bin/date '+%H:%M:%S') USB event monitor could not restart after wake; polling fallback remains active."
    return 1
}

retry_usb_event_monitor_if_due() {
    local now
    [[ "$EVENT_MONITOR_RETRY_ENABLED" == true ]] || return 1
    [[ "$EVENT_MONITOR_ACTIVE" != true ]] || return 0
    now=$(wall_time) || return 1
    (( now - EVENT_LAST_START_ATTEMPT >= EVENT_RETRY_SECONDS )) || return 1

    if start_usb_event_monitor; then
        echo "$(/bin/date '+%H:%M:%S') USB event monitor restored after polling fallback."
        return 0
    fi
    return 1
}

read_event_with_timeout() {
    local timeout="$1"
    /usr/bin/perl -e '
        use strict;
        use warnings;
        use Time::HiRes qw(time);
        my ($fd, $timeout) = @ARGV;
        open(my $stream, "<&=$fd") or exit 2;
        my $deadline = time() + $timeout;
        my $line = "";
        while (length($line) <= 64) {
            my $remaining = $deadline - time();
            exit(length($line) == 0 ? 1 : 2) if $remaining <= 0;
            my $readable = "";
            vec($readable, fileno($stream), 1) = 1;
            my $ready = select($readable, undef, undef, $remaining);
            exit 3 unless defined $ready;
            exit(length($line) == 0 ? 1 : 2) if $ready == 0;
            my $count = sysread($stream, my $byte, 1);
            exit 2 unless defined($count) && $count == 1;
            $line .= $byte;
            if ($byte eq "\n") {
                print $line;
                exit 0;
            }
        }
        exit 2;
    ' "$EVENT_MONITOR_FD" "$timeout"
}

wait_for_fast_check() {
    local cycle_started_wall="${1:-}" message now_wall read_status
    if [[ -z "$cycle_started_wall" ]]; then
        cycle_started_wall=$(wall_time) || cycle_started_wall=0
    fi
    if [[ "$EVENT_MONITOR_ACTIVE" != true ]]; then
        /bin/sleep "$FAST_INTERVAL"
        return 0
    fi

    if message=$(read_event_with_timeout "$FAST_INTERVAL"); then
        case "$message" in
            usb-published|usb-terminated)
                EVENT_LAST_HEARTBEAT=$SECONDS
                return 0
                ;;
            heartbeat)
                EVENT_LAST_HEARTBEAT=$SECONDS
                return 0
                ;;
            *)
                mark_event_monitor_unavailable "invalid event message"
                return 0
                ;;
        esac
    else
        read_status=$?
    fi

    if ! /bin/kill -0 "$EVENT_MONITOR_PID" 2>/dev/null; then
        mark_event_monitor_unavailable "listener process exited"
        return 0
    fi

    # A still-running helper can return a timeout or interrupted stream as the
    # system wakes. Let the outer loop recognize that sleep interval and replace
    # the listener instead of treating this first read result as a lasting fault.
    now_wall=$(wall_time) || now_wall="$cycle_started_wall"
    if (( now_wall - cycle_started_wall > WAKE_GAP_SECONDS )); then
        EVENT_LAST_HEARTBEAT=$SECONDS
        return 0
    fi

    if (( read_status != 1 )); then
        mark_event_monitor_unavailable "event stream read failure $read_status"
        return 0
    fi

    if (( SECONDS - EVENT_LAST_HEARTBEAT > EVENT_HEARTBEAT_TIMEOUT_SECONDS )); then
        mark_event_monitor_unavailable "listener heartbeat stale"
    fi
}

parse_usb_snapshot() {
    /usr/bin/awk '
        function observed(value) {
            return (value != "" ? value : "?")
        }
        function property_value(line, key, value) {
            value = line
            sub(".*\\\"" key "\\\" = ", "", value)
            gsub(/\"/, "", value)
            return value
        }
        function flush_interface(tuple) {
            if (!in_interface) return
            tuple = observed(inumber) ":" \
                observed(iclass) "/" observed(isubclass) "/" observed(iprotocol)
            interface_count++
            interface_values[interface_count] = tuple
            in_interface = 0
            inumber=""; iclass=""; isubclass=""; iprotocol=""
        }
        function sorted_interfaces(    i, j, temporary, result) {
            for (i = 1; i <= interface_count; i++) {
                for (j = i + 1; j <= interface_count; j++) {
                    if (interface_values[j] < interface_values[i]) {
                        temporary = interface_values[i]
                        interface_values[i] = interface_values[j]
                        interface_values[j] = temporary
                    }
                }
            }
            result = ""
            for (i = 1; i <= interface_count; i++) {
                result = result (result != "" ? "," : "") interface_values[i]
            }
            return result
        }
        function clear_interfaces(    i) {
            for (i = 1; i <= interface_count; i++) delete interface_values[i]
            interface_count = 0
        }
        function flush(interfaces, profile) {
            flush_interface()
            if (in_device && name != "" && vid != "") {
                pn = (prod != "" ? prod : name)
                interfaces = sorted_interfaces()
                profile = "usb=" observed(usb_version) ";rev=" observed(revision) \
                    ";device=" observed(dclass) "/" observed(dsubclass) "/" observed(dprotocol) \
                    ";packet=" observed(max_packet) ";configs=" observed(configs) \
                    ";interfaces=" (interfaces != "" ? interfaces : "none")
                print "USB:port=" observed(loc) " " observed(vid) ":" observed(pid) \
                    " sn=" sn " " pn " | profile=" profile
            }
            in_device=0; in_interface=0; device_depth=0
            name=""; vid=""; pid=""; loc=""; sn=""; prod=""
            usb_version=""; revision=""; dclass=""; dsubclass=""; dprotocol=""
            max_packet=""; configs=""; interfaces=""
            clear_interfaces()
        }
        /\+-o / {
            depth = index($0, "+-o ")
            if ($0 ~ /<class [^,>]*USBHostDevice/) {
                flush()
                in_device=1; device_depth=depth
                name = $0
                sub(/.*\+-o /, "", name); sub(/@.*/, "", name); sub(/  <class.*/, "", name)
                gsub(/^[ \t]+|[ \t]+$/, "", name)
            } else if (in_device && depth > device_depth &&
                       $0 ~ /<class [^,>]*USBHostInterface/) {
                flush_interface()
                in_interface=1
            } else {
                flush_interface()
                if (in_device && depth <= device_depth) flush()
            }
            next
        }
        in_device && !in_interface && /"idVendor" = /  { vid = $NF }
        in_device && !in_interface && /"idProduct" = / { pid = $NF }
        in_device && !in_interface && /"locationID" = / { loc = $NF }
        in_device && !in_interface && /"bcdUSB" = / { usb_version = $NF }
        in_device && !in_interface && /"bcdDevice" = / { revision = $NF }
        in_device && !in_interface && /"bDeviceClass" = / { dclass = $NF }
        in_device && !in_interface && /"bDeviceSubClass" = / { dsubclass = $NF }
        in_device && !in_interface && /"bDeviceProtocol" = / { dprotocol = $NF }
        in_device && !in_interface && /"bMaxPacketSize0" = / { max_packet = $NF }
        in_device && !in_interface && /"bNumConfigurations" = / { configs = $NF }
        in_device && !in_interface && /"(USB Serial Number|kUSBSerialNumberString)" = / {
            if ($0 ~ /"USB Serial Number" = /) {
                sn = property_value($0, "USB Serial Number")
            } else {
                sn = property_value($0, "kUSBSerialNumberString")
            }
        }
        in_device && !in_interface && /"(USB Product Name|kUSBProductString)" = / {
            if ($0 ~ /"USB Product Name" = /) {
                prod = property_value($0, "USB Product Name")
            } else {
                prod = property_value($0, "kUSBProductString")
            }
        }
        in_interface && /"bInterfaceNumber" = / { inumber = $NF }
        in_interface && /"bInterfaceClass" = / { iclass = $NF }
        in_interface && /"bInterfaceSubClass" = / { isubclass = $NF }
        in_interface && /"bInterfaceProtocol" = / { iprotocol = $NF }
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
            printf 'shutdown_policy=%s\n' "$SHUTDOWN_POLICY"
            printf 'status=%s\n' "$status"
            printf 'heartbeat=%s\n' "$now"
            printf 'detail=%s\n' "$detail"
        } > "$temp"
    )
    /bin/chmod "$file_mode" "$temp"
    /bin/mv -f "$temp" "$STATE_FILE"
    LAST_HEARTBEAT="$now"
}

write_monitoring_state() {
    if [[ "$EVENT_MONITOR_HEALTHY" == true ]]; then
        write_state "ready" "event-triggered USB checks with polling fallback"
    else
        write_state "polling" "USB event monitor unavailable; polling fallback active"
    fi
}

write_heartbeat() {
    local now
    [[ -n "$STATE_FILE" ]] || return 0
    now=$(/bin/date +%s)
    if [[ "$now" != "$LAST_HEARTBEAT" ]]; then
        if [[ "$FAST_HEALTHY" == true && "$SLOW_HEALTHY" == true ]]; then
            write_monitoring_state
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

request_graceful_shutdown() {
    /sbin/shutdown -h now || true
}

request_forced_halt() {
    /sbin/halt -q || true
}

wait_before_forced_halt() {
    /bin/sleep "$GRACEFUL_SHUTDOWN_SECONDS"
}

force_halt_forever() {
    while true; do
        request_forced_halt
        /bin/sleep 1
    done
}

do_shutdown() {
    local reason="$1" state_detail="${2:-shutdown required}"
    write_state "shutting-down" "$state_detail" || true

    if [[ "$DRY_RUN" == true ]]; then
        echo ""
        echo "$(/bin/date '+%H:%M:%S') !!! DRY RUN — shutdown required !!!"
        echo "  Reason: $reason"
        echo "  Policy: $SHUTDOWN_POLICY"
        return 1
    fi

    echo "!!! WATCHDOG TRIGGERED: $reason"
    if [[ "$SHUTDOWN_POLICY" == "force-immediately" ]]; then
        echo "!!! STARTING FORCED HALT NOW !!!"
        force_halt_forever
        return 0
    fi

    echo "!!! REQUESTING GRACEFUL SHUTDOWN NOW !!!"
    request_graceful_shutdown
    wait_before_forced_halt
    force_halt_forever
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
    do_shutdown "$reason" "hardware inventory change detected"
}

runtime_probe_fault() {
    local probe_group="$1"
    write_state "fault" "$probe_group probe failed or timed out"
    if [[ "$DRY_RUN" == true ]]; then
        echo "$(/bin/date '+%H:%M:%S') DRY RUN — $probe_group probe fault; retaining last known-good baseline."
        /bin/sleep 1
        return 1
    fi
    do_shutdown \
        "PROBE FAULT: $probe_group inventory failed after bounded retries" \
        "hardware inventory probe failure"
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
    local fast_current slow_current cycle last_cycle_wall now_wall
    cycle=0
    last_cycle_wall=$(wall_time) || return 1

    while true; do
        retry_usb_event_monitor_if_due || true
        wait_for_fast_check "$last_cycle_wall"
        stop_requested && return 0
        now_wall=$(wall_time) || return 1

        if (( now_wall - last_cycle_wall > WAKE_GAP_SECONDS )); then
            write_state "settling" "system wake detected"
            /bin/sleep "$WAKE_SETTLE_SECONDS"
            # Re-register IOKit notifications after every wake. The listener can
            # exit or its stream can become unusable across system sleep even
            # while the independently timed polling engine remains healthy.
            restart_usb_event_monitor_after_wake || true
            FAST_HEALTHY=false
            SLOW_HEALTHY=false
            fast_current=$(collect_stable_snapshot fast) || {
                stop_requested && return 0
                runtime_probe_fault "post-wake USB/Thunderbolt" || true
                last_cycle_wall=$(wall_time) || return 1
                continue
            }
            FAST_HEALTHY=true
            slow_current=$(collect_stable_snapshot slow) || {
                stop_requested && return 0
                runtime_probe_fault "post-wake SD/display" || true
                last_cycle_wall=$(wall_time) || return 1
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
            write_monitoring_state
            cycle=0
            last_cycle_wall=$(wall_time) || return 1
            continue
        fi

        if ! fast_current=$(get_fast_snapshot); then
            FAST_HEALTHY=false
            write_state "degraded" "USB/Thunderbolt probe retrying"
            fast_current=$(read_snapshot_with_retries fast) || {
                runtime_probe_fault "USB/Thunderbolt" || true
                last_cycle_wall=$(wall_time) || return 1
                continue
            }
        fi
        if [[ "$fast_current" != "$FAST_BASE" ]]; then
            # A successful difference is already a security event. In real
            # mode process_change does not return after starting shutdown. A
            # dry run returns, so confirm only to choose its next baseline;
            # never let confirmation erase the event already observed.
            if ! process_change "$FAST_BASE" "$fast_current"; then
                fast_current=$(read_snapshot_with_retries fast) || {
                    FAST_HEALTHY=false
                    runtime_probe_fault "USB/Thunderbolt confirmation" || true
                    last_cycle_wall=$(wall_time) || return 1
                    continue
                }
                if [[ "$fast_current" != "$FAST_BASE" ]]; then
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
                    last_cycle_wall=$(wall_time) || return 1
                    continue
                }
            fi
            if [[ "$slow_current" != "$SLOW_BASE" ]]; then
                # Match the fast path: enforce the first successful change,
                # then use confirmation only for the dry-run baseline.
                if ! process_change "$SLOW_BASE" "$slow_current"; then
                    slow_current=$(read_snapshot_with_retries slow) || {
                        SLOW_HEALTHY=false
                        runtime_probe_fault "SD/display confirmation" || true
                        last_cycle_wall=$(wall_time) || return 1
                        continue
                    }
                    if [[ "$slow_current" != "$SLOW_BASE" ]]; then
                        SLOW_BASE="$slow_current"
                        echo "$(/bin/date '+%H:%M:%S') Dry-run baseline updated."
                    fi
                fi
            fi
            SLOW_HEALTHY=true
        fi

        write_heartbeat
        last_cycle_wall=$(wall_time) || return 1
    done
}

main() {
    local combined count fast_current
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
    trap 'stop_usb_event_monitor; remove_owned_state' EXIT
    trap signal_exit SIGINT SIGTERM

    if ! initialize_baselines; then
        stop_requested && exit 0
        write_state "fault" "could not establish a complete stable baseline" || true
        echo "Error: could not establish a complete stable hardware baseline." >&2
        exit 2
    fi

    EVENT_MONITOR_RETRY_ENABLED=true
    if ! start_usb_event_monitor; then
        echo "$(/bin/date '+%H:%M:%S') Native USB event monitor unavailable; using polling only."
    fi

    # Close the short handoff between the stable baseline and event-listener
    # registration. A persistent change in that window is enforced before the
    # engine reports ready; later helper events remain queued for the loop.
    fast_current=$(collect_stable_snapshot fast) || {
        write_state "fault" "could not reconcile USB event monitor startup" || true
        echo "Error: could not reconcile the USB event monitor with the baseline." >&2
        exit 2
    }
    if [[ "$fast_current" != "$FAST_BASE" ]]; then
        if ! process_change "$FAST_BASE" "$fast_current"; then
            FAST_BASE="$fast_current"
        fi
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
    if [[ "$EVENT_MONITOR_HEALTHY" == true ]]; then
        echo "USB events:     native event-triggered checks active"
    else
        echo "USB events:     unavailable; polling fallback active"
    fi
    echo "USB/TB fallback: every ${FAST_INTERVAL}s plus probe time"
    echo "SD/display:     every $((SLOW_CYCLES)) fast cycles plus probe time"
    echo "After wake:     compare a new stable snapshot"
    echo "Dry run:        $DRY_RUN"
    echo "Shutdown:       $SHUTDOWN_POLICY"
    echo ""
    echo "$(/bin/date '+%H:%M:%S') Ready. Monitoring validated snapshots."

    write_monitoring_state
    monitor_loop
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
