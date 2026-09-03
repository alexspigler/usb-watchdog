#!/usr/bin/env python3
"""macOS menu-bar controller for the USB Watchdog shell engine."""

import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
import time

try:
    import rumps
except ImportError:
    sys.exit(
        "Missing dependency 'rumps'. Install it with:\n"
        "    python3 -m pip install -r requirements.txt"
    )


if getattr(sys, "frozen", False):
    RESOURCES = os.path.normpath(
        os.path.join(os.path.dirname(sys.executable), "..", "Resources")
    )
    SCRIPT = os.path.join(RESOURCES, "usb_watchdog.sh")
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
    SCRIPT = os.path.join(HERE, "usb_watchdog.sh")

USER_SUPPORT_DIR = os.path.expanduser("~/Library/Application Support/USB Watchdog")
ROOT_STATE = "/var/run/usb-watchdog.state"
DRY_STATE = os.path.join(USER_SUPPORT_DIR, "dry-run.state")
ROOT_LOG = "/var/log/usb_watchdog.log"
DRY_LOG = os.path.expanduser("~/Library/Logs/usb_watchdog.log")

ICON_DISARMED = "🔴"
ICON_ARMED = "🟢"
ICON_DRYRUN = "🟡"
ICON_FAULT = "🟠"

STATE_TOKEN_RE = re.compile(r"^[A-Fa-f0-9-]{16,64}$")
HEARTBEAT_STALE_SECONDS = 12
# Four attempts, two snapshots per attempt, and four independently bounded
# probes across the fast and slow groups can take about 96 seconds in the
# worst case. Leave margin for scheduling and state-file publication.
ARM_TIMEOUT_SECONDS = 120
PS_COMMAND = ["/usr/bin/env", "TZ=UTC", "LC_ALL=C", "/bin/ps"]
GRACEFUL_THEN_FORCE = "graceful-then-force"
FORCE_IMMEDIATELY = "force-immediately"
SHUTDOWN_POLICIES = {GRACEFUL_THEN_FORCE, FORCE_IMMEDIATELY}
DEFAULT_DRY_RUN = True


def sh(command, timeout=10):
    """Run an argv list and return (returncode, stdout, stderr), always bounded."""
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return completed.returncode, completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return 124, stdout, stderr or "command timed out"
    except OSError as exc:
        return 127, "", str(exc)


def shell_join(command):
    """Quote fixed argv for the AppleScript administrator shell boundary."""
    return " ".join(shlex.quote(str(part)) for part in command)


def open_private_log(log_path):
    """Open an append-only owner-readable log without following a symlink."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(log_path, flags, 0o600)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("watchdog log path is not a regular file")
        os.fchmod(descriptor, 0o600)
        return os.fdopen(descriptor, "ab", buffering=0)
    except Exception:
        os.close(descriptor)
        raise


def privileged_log_launch(command, log_path):
    """Build the fixed root launch command with a private, regular log file."""
    quoted_log = shlex.quote(log_path)
    unsafe_log_check = (
        "if [ -L {0} ] || {{ [ -e {0} ] && [ ! -f {0} ]; }}; then "
        "echo 'Refusing unsafe watchdog log path' >&2; exit 1; fi"
    ).format(quoted_log)
    return (
        "umask 077; {check}; /usr/bin/touch {log} && /bin/chmod 600 {log} && "
        "( {command} </dev/null >>{log} 2>&1 & )"
    ).format(
        check=unsafe_log_check,
        command=shell_join(command),
        log=quoted_log,
    )


def osascript_admin_shell(shell_command):
    """Run one already-quoted shell command behind the native admin prompt."""
    escaped = shell_command.replace("\\", "\\\\").replace('"', '\\"')
    apple = 'do shell script "%s" with administrator privileges' % escaped
    rc, _, stderr = sh(["/usr/bin/osascript", "-e", apple], timeout=45)
    return rc == 0, stderr


def osascript_admin(command):
    return osascript_admin_shell(shell_join(command))


def _state_owner_is_valid(state_path, owner_uid):
    if state_path == ROOT_STATE:
        return owner_uid == 0
    return owner_uid == os.getuid()


def _read_state_data(state_path):
    """Read a regular, correctly owned state file without following symlinks."""
    try:
        info = os.lstat(state_path)
        if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            return None
        if not _state_owner_is_valid(state_path, info.st_uid):
            return None
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return None
        with open(state_path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None

    data = {}
    for line in lines:
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        data[key] = value

    try:
        pid = int(data["pid"])
        uid = int(data["uid"])
        heartbeat = int(data["heartbeat"])
    except (KeyError, TypeError, ValueError):
        return None

    token = data.get("token", "")
    started = data.get("started", "").strip()
    mode = data.get("mode", "")
    status = data.get("status", "unknown")
    shutdown_policy = data.get("shutdown_policy", GRACEFUL_THEN_FORCE)
    if (
        data.get("version") != "1"
        or pid <= 1
        or uid < 0
        or not STATE_TOKEN_RE.fullmatch(token)
        or not started
    ):
        return None
    if mode not in {"real", "dry-run"}:
        return None
    if shutdown_policy not in SHUTDOWN_POLICIES:
        return None

    data.update(
        {
            "path": state_path,
            "pid": pid,
            "uid": uid,
            "heartbeat": heartbeat,
            "token": token,
            "started": started,
            "mode": mode,
            "status": status,
            "shutdown_policy": shutdown_policy,
        }
    )
    return data


def _process_matches_state(state):
    """Validate PID, UID, engine name, and per-launch token together."""
    rc, output, _ = sh(
        PS_COMMAND
        + ["-ww", "-p", str(state["pid"]), "-o", "uid=,command="],
        timeout=2,
    )
    if rc != 0 or not output.strip():
        return False
    parts = output.strip().split(None, 1)
    if len(parts) != 2:
        return False
    try:
        process_uid = int(parts[0])
    except ValueError:
        return False
    command = parts[1]
    marker = "--instance-token %s" % state["token"]
    command_matches = (
        process_uid == state["uid"]
        and "usb_watchdog.sh" in command
        and marker in command
    )
    if not command_matches:
        return False
    rc, started, _ = sh(
        PS_COMMAND + ["-p", str(state["pid"]), "-o", "lstart="], timeout=2
    )
    return rc == 0 and started.strip() == state["started"]


def read_instance(state_path, now=None):
    """Return registered instance state, including stale/faulted records."""
    state = _read_state_data(state_path)
    if state is None:
        return None
    if now is None:
        now = int(time.time())
    state["alive"] = _process_matches_state(state)
    state["age"] = now - state["heartbeat"]
    state["healthy"] = (
        state["alive"]
        and state["status"] == "ready"
        and 0 <= state["age"] <= HEARTBEAT_STALE_SECONDS
    )
    return state


def watchdog_instances():
    """Inspect only the two state files owned by this controller."""
    instances = []
    for state_path in (ROOT_STATE, DRY_STATE):
        instance = read_instance(state_path)
        if instance is not None:
            instances.append(instance)
    return instances


def pretty_device(line):
    if line.startswith("USB:"):
        match = re.match(r"USB:port=(\S+) (\S+) sn=(\S*) (.+)", line)
        if match:
            location, _vendor_product, serial, name = match.groups()
            serial_tag = " · #%s" % serial if serial else ""
            return "USB · %s (port %s)%s" % (name, location, serial_tag)
        return "USB · " + line[4:]
    if line.startswith("TB:"):
        match = re.match(r"TB:uid=(\S+) (.+)", line)
        return "Thunderbolt · " + (match.group(2) if match else line[3:])
    if line.startswith("SD:"):
        return "SD card · " + line[3:]
    if line.startswith("DISPLAY:"):
        return "Display · " + line[8:]
    return line


def notify(title, subtitle, message):
    try:
        rumps.notification(title, subtitle, message)
    except Exception:
        pass


class WatchdogApp(rumps.App):
    def __init__(self):
        super().__init__(ICON_DISARMED, quit_button=None)
        self.dry_run = DEFAULT_DRY_RUN
        self.shutdown_policy = GRACEFUL_THEN_FORCE
        self._tick = 0
        self._previously_healthy = False
        self._fault_notified = False

        self.status_item = rumps.MenuItem("● Disarmed")
        self.status_item.set_callback(None)
        self.toggle_item = rumps.MenuItem("Arm", callback=self.on_toggle)
        self.dryrun_item = rumps.MenuItem(
            "Dry-run (test, no shutdown)", callback=self.on_toggle_dryrun
        )
        self.dryrun_item.state = 1 if self.dry_run else 0
        self.shutdown_menu = rumps.MenuItem("Shutdown response")
        self.graceful_shutdown_item = rumps.MenuItem(
            "Graceful shutdown, then forced halt (recommended)",
            callback=self.on_select_graceful_shutdown,
        )
        self.immediate_halt_item = rumps.MenuItem(
            "Immediate forced halt (unsafe)",
            callback=self.on_select_immediate_halt,
        )
        self.shutdown_menu.add(self.graceful_shutdown_item)
        self.shutdown_menu.add(self.immediate_halt_item)
        self._update_shutdown_menu()
        self.devices_menu = rumps.MenuItem("Devices")
        self.devices_menu.add(rumps.MenuItem("(scanning…)"))

        self.menu = [
            self.status_item,
            None,
            self.toggle_item,
            None,
            self.dryrun_item,
            self.shutdown_menu,
            None,
            self.devices_menu,
            None,
            rumps.MenuItem("Quit", callback=self.on_quit),
        ]

        self.timer = rumps.Timer(self.refresh, 4)
        self.timer.start()
        self.refresh(None)

    def on_toggle_dryrun(self, sender):
        self.dry_run = not self.dry_run
        sender.state = 1 if self.dry_run else 0

    def on_select_graceful_shutdown(self, _):
        self.shutdown_policy = GRACEFUL_THEN_FORCE
        self._update_shutdown_menu()

    def on_select_immediate_halt(self, _):
        self.shutdown_policy = FORCE_IMMEDIATELY
        self._update_shutdown_menu()

    def _update_shutdown_menu(self):
        self.graceful_shutdown_item.state = (
            1 if self.shutdown_policy == GRACEFUL_THEN_FORCE else 0
        )
        self.immediate_halt_item.state = (
            1 if self.shutdown_policy == FORCE_IMMEDIATELY else 0
        )

    def _confirm_real_mode(self):
        if self.shutdown_policy == FORCE_IMMEDIATELY:
            response = "immediately request an ungraceful forced halt"
        else:
            response = (
                "request a normal shutdown, then force a halt after "
                "the five-second grace period"
            )
        result = rumps.alert(
            "Arm real shutdown mode?",
            "A successfully observed inventory change or persistent probe failure "
            "will %s. Unsaved work can be lost." % response,
            ok="Arm real mode",
            cancel="Cancel",
        )
        return result == 1

    def on_toggle(self, _):
        if watchdog_instances():
            self.disarm()
        else:
            self.arm()

    def _launch_arguments(self, token, state_path):
        arguments = [
            "/bin/bash",
            SCRIPT,
            "--state-file",
            state_path,
            "--instance-token",
            token,
            "--shutdown-policy",
            self.shutdown_policy,
        ]
        if self.dry_run:
            arguments.append("--dry-run")
        return arguments

    def arm(self):
        if not os.path.isfile(SCRIPT):
            rumps.alert("Cannot find usb_watchdog.sh", "Expected at:\n" + SCRIPT)
            return
        if self.shutdown_policy not in SHUTDOWN_POLICIES:
            rumps.alert("Cannot arm", "The selected shutdown response is invalid.")
            return
        if not self.dry_run and not self._confirm_real_mode():
            return

        token = secrets.token_hex(16)
        state_path = DRY_STATE if self.dry_run else ROOT_STATE
        log_path = DRY_LOG if self.dry_run else ROOT_LOG
        arguments = self._launch_arguments(token, state_path)

        if self.dry_run:
            os.makedirs(os.path.dirname(DRY_STATE), mode=0o700, exist_ok=True)
            os.makedirs(os.path.dirname(DRY_LOG), mode=0o700, exist_ok=True)
            try:
                with open_private_log(log_path) as log_handle:
                    subprocess.Popen(
                        arguments,
                        stdin=subprocess.DEVNULL,
                        stdout=log_handle,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                launched = True
                launch_error = ""
            except OSError as exc:
                launched = False
                launch_error = str(exc)
        else:
            launch = privileged_log_launch(arguments, log_path)
            launched, launch_error = osascript_admin_shell(launch)

        armed = False
        last_state = None
        if launched:
            deadline = time.monotonic() + ARM_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                last_state = read_instance(state_path)
                if (
                    last_state
                    and last_state["token"] == token
                    and last_state["healthy"]
                ):
                    armed = True
                    break
                time.sleep(0.25)

        if armed:
            mode_text = "dry-run" if self.dry_run else "real"
            notify(
                "USB Watchdog",
                "Armed",
                "Validated baseline ready in %s mode." % mode_text,
            )
        else:
            detail = launch_error.strip()
            if last_state:
                detail = last_state.get("detail", "") or last_state.get("status", "")
            if not detail:
                try:
                    with open(log_path, encoding="utf-8", errors="replace") as handle:
                        detail = handle.read()[-800:].strip()
                except OSError:
                    detail = ""
            rumps.alert(
                "Arm failed",
                "The watchdog never reported a healthy, validated baseline.\n\n%s"
                % (detail or "No diagnostic output was available."),
            )
        self.refresh(None)

    def _stop_instance(self, instance):
        command = [
            "/bin/bash",
            SCRIPT,
            "--stop",
            "--state-file",
            instance["path"],
            "--instance-token",
            instance["token"],
        ]
        if instance["uid"] == 0:
            return osascript_admin(command)
        rc, _, stderr = sh(command, timeout=25)
        return rc == 0, stderr

    def disarm(self):
        instances = watchdog_instances()
        if not instances:
            return

        errors = []
        for instance in instances:
            stopped, error = self._stop_instance(instance)
            if not stopped:
                errors.append(error.strip() or "Could not stop PID %s" % instance["pid"])

        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and watchdog_instances():
            time.sleep(0.2)
        leftovers = watchdog_instances()
        if leftovers:
            errors.append(
                "Registered watchdog state remains for PID(s): %s"
                % ", ".join(str(item["pid"]) for item in leftovers)
            )

        if errors:
            rumps.alert("Disarm did not complete", "\n\n".join(errors))
        else:
            notify("USB Watchdog", "Disarmed", "Exact registered monitoring stopped.")
            self._previously_healthy = False
            self._fault_notified = False
        self.refresh(None)

    def on_quit(self, _):
        if watchdog_instances():
            result = rumps.alert(
                "Quit the controller?",
                "A registered watchdog remains active or needs attention after the "
                "menu app quits. Disarm first if monitoring should stop.",
                ok="Quit anyway",
                cancel="Cancel",
            )
            if result != 1:
                return
        rumps.quit_application()

    def refresh(self, _):
        instances = watchdog_instances()
        healthy = [item for item in instances if item["healthy"]]
        unhealthy = [item for item in instances if not item["healthy"]]

        if instances:
            registered = healthy[0] if healthy else unhealthy[0]
            self.shutdown_policy = registered["shutdown_policy"]
            self._update_shutdown_menu()

        if healthy:
            real = any(item["mode"] == "real" for item in healthy)
            self.title = ICON_ARMED if real else ICON_DRYRUN
            mode = "real" if real else "dry-run"
            self.status_item.title = "● Armed (%s) — validated and healthy" % mode
            self.toggle_item.title = "Disarm"
            self._set_settings_enabled(False)
            self._previously_healthy = True
            self._fault_notified = False
        elif unhealthy:
            current = unhealthy[0]
            self.title = ICON_FAULT
            self.status_item.title = "● Attention required — %s" % current.get(
                "status", "unhealthy"
            )
            self.toggle_item.title = "Disarm"
            self._set_settings_enabled(False)
            if self._previously_healthy and not self._fault_notified:
                notify(
                    "USB Watchdog",
                    "Monitoring fault",
                    "The registered watchdog is no longer reporting healthy monitoring.",
                )
                self._fault_notified = True
        else:
            self.title = ICON_DISARMED
            self.status_item.title = "● Disarmed"
            self.toggle_item.title = "Arm"
            self._set_settings_enabled(True)
            if self._previously_healthy and not self._fault_notified:
                notify(
                    "USB Watchdog",
                    "Monitoring stopped unexpectedly",
                    "No registered watchdog instance remains.",
                )
                self._fault_notified = True

        if self._tick % 4 == 0:
            self._update_devices()
        self._tick += 1

    def _set_settings_enabled(self, enabled):
        self.dryrun_item.set_callback(self.on_toggle_dryrun if enabled else None)
        self.graceful_shutdown_item.set_callback(
            self.on_select_graceful_shutdown if enabled else None
        )
        self.immediate_halt_item.set_callback(
            self.on_select_immediate_halt if enabled else None
        )

    def _update_devices(self):
        rc, output, error = sh(["/bin/bash", SCRIPT, "--snapshot"], timeout=10)
        self.devices_menu.clear()
        if rc != 0:
            self.devices_menu.title = "Devices (unavailable)"
            item = rumps.MenuItem("(hardware inventory unavailable: %s)" % (error.strip() or "probe failed"))
            item.set_callback(None)
            self.devices_menu.add(item)
            return

        devices = [line for line in output.splitlines() if line.strip()]
        self.devices_menu.title = "Devices (%d)" % len(devices)
        if devices:
            for device in devices:
                self.devices_menu.add(rumps.MenuItem(pretty_device(device)))
        else:
            item = rumps.MenuItem("(no observed peripherals connected)")
            item.set_callback(None)
            self.devices_menu.add(item)


if __name__ == "__main__":
    WatchdogApp().run()
