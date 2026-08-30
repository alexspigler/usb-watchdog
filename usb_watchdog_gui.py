#!/usr/bin/env python3
"""
usb_watchdog_gui.py — macOS menu bar controller for usb_watchdog.sh

A small rumps menu bar app that arms/disarms the USB/Thunderbolt/SD/HDMI
"dead man's switch". The bash script is the detection + shutdown engine; this
is the controller.

The real watchdog runs as root (so killing it needs your admin password) and
runs independently of this menu-bar app — quitting the app does NOT stop
monitoring. Detection is INSTANT: any device change forces an immediate
/sbin/halt, with no delay and no way to cancel. Dry-run mode runs as your
user (no prompt, no shutdown) for quick testing.

  Install:  python3 -m pip install rumps
  Run:      python3 usb_watchdog_gui.py
"""

import os
import re
import subprocess
import sys
import time

try:
    import rumps
except ImportError:
    sys.exit("Missing dependency 'rumps'. Install it with:\n"
             "    python3 -m pip install rumps")

if getattr(sys, "frozen", False):
    # Running inside a py2app bundle: the script is a bundled resource.
    _RESOURCES = os.path.normpath(
        os.path.join(os.path.dirname(sys.executable), "..", "Resources"))
    SCRIPT = os.path.join(_RESOURCES, "usb_watchdog.sh")
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
    SCRIPT = os.path.join(HERE, "usb_watchdog.sh")
# Per-mode log paths. Root logs where only root can write — a fixed /tmp
# path would let any local user pre-plant a symlink for root to clobber.
# Dry-run logs under the user's own Library/Logs, so a root-owned leftover
# can never block the redirect and silently kill the launch.
LOG_ROOT = "/var/log/usb_watchdog.log"
LOG_DRYRUN = os.path.expanduser("~/Library/Logs/usb_watchdog.log")

ICON_DISARMED = "🔴"   # not armed
ICON_ARMED = "🟢"      # armed
ICON_DRYRUN = "🟡"     # armed in dry-run (test) mode


def sh(cmd):
    """Run a command list; return (returncode, stdout, stderr).
    Decode UTF-8 explicitly: inside a py2app bundle the default locale
    encoding is ASCII, which crashes on non-ASCII process/device names."""
    p = subprocess.run(cmd, capture_output=True,
                       encoding="utf-8", errors="replace")
    return p.returncode, p.stdout, p.stderr


def osascript_admin(shell_cmd):
    """Run a shell command with the native admin password prompt.
    Returns True on success, False if the user cancels or it fails."""
    escaped = shell_cmd.replace("\\", "\\\\").replace('"', '\\"')
    apple = 'do shell script "%s" with administrator privileges' % escaped
    rc, _, _ = sh(["osascript", "-e", apple])
    return rc == 0


def watchdog_pids():
    """PIDs of running monitor processes. Excludes the --snapshot helper,
    this GUI (its command line never contains 'usb_watchdog.sh'), and the
    monitor's own transient command-substitution forks — those forks show
    the same command line, so any match whose parent is also a match is
    dropped rather than counted as a separate watchdog."""
    rc, out, _ = sh(["ps", "-axo", "pid=,ppid=,command="])
    matches = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3:
            continue
        pid, ppid, cmd = parts
        if "usb_watchdog.sh" in cmd and "--snapshot" not in cmd:
            matches.append((int(pid), int(ppid)))
    all_pids = {pid for pid, _ in matches}
    return [pid for pid, ppid in matches if ppid not in all_pids]


def pid_is_root(pid):
    rc, out, _ = sh(["ps", "-o", "user=", "-p", str(pid)])
    return rc == 0 and out.strip() == "root"


def pretty_device(line):
    if line.startswith("USB:"):
        # USB:port=<loc> <vid>:<pid> sn=<serial> <name>
        m = re.match(r"USB:port=(\S+) (\S+) sn=(\S*) (.+)", line)
        if m:
            loc, _vidpid, sn, name = m.groups()
            tag = " · #%s" % sn if sn else ""
            return "USB · %s (port %s)%s" % (name, loc, tag)
        return "USB · " + line[4:]
    if line.startswith("TB:"):
        # TB:uid=<uid> <vendor> / <name>
        m = re.match(r"TB:uid=(\S+) (.+)", line)
        if m:
            return "Thunderbolt · " + m.group(2)
        return "Thunderbolt · " + line[3:]
    if line.startswith("SD:"):
        return "SD card · " + line[3:]
    if line.startswith("DISPLAY:"):
        return "Display · " + line[8:]
    return line


def notify(title, subtitle, message):
    """Best-effort notification; silently ignored if not bundled."""
    try:
        rumps.notification(title, subtitle, message)
    except Exception:
        pass


class WatchdogApp(rumps.App):
    def __init__(self):
        super().__init__(ICON_DISARMED, quit_button=None)
        self.dry_run = False

        self.status_item = rumps.MenuItem("● Disarmed")
        self.status_item.set_callback(None)  # info only
        self.toggle_item = rumps.MenuItem("Arm", callback=self.on_toggle)

        self.dryrun_item = rumps.MenuItem("Dry-run (test, no shutdown)",
                                          callback=self.on_toggle_dryrun)

        self.devices_menu = rumps.MenuItem("Devices")
        # Seed the submenu so its NSMenu exists before refresh() calls clear().
        self.devices_menu.add(rumps.MenuItem("(scanning…)"))

        self.menu = [
            self.status_item,
            None,
            self.toggle_item,
            None,
            self.dryrun_item,
            None,
            self.devices_menu,
            None,
            rumps.MenuItem("Quit", callback=self.on_quit),
        ]

        # Menu-display refresh only (status icon + device list). This is
        # independent of the watchdog's own detection loop, so a slower cadence
        # here saves idle CPU with zero effect on how fast it reacts.
        self._tick = 0
        self.timer = rumps.Timer(self.refresh, 4)
        self.timer.start()
        self.refresh(None)

    # --- settings ---
    def on_toggle_dryrun(self, sender):
        self.dry_run = not self.dry_run
        sender.state = 1 if self.dry_run else 0

    # --- arm / disarm ---
    def on_toggle(self, _):
        if watchdog_pids():
            self.disarm()
        else:
            self.arm()

    def arm(self):
        if not os.path.exists(SCRIPT):
            rumps.alert("Cannot find usb_watchdog.sh", "Expected at:\n" + SCRIPT)
            return
        cmd_args = "--dry-run" if self.dry_run else ""
        log = LOG_DRYRUN if self.dry_run else LOG_ROOT
        # Subshell backgrounding (no nohup): nohup fails with "can't detach
        # from console" when launched via the admin-privilege mechanism,
        # which has no controlling terminal. ( ... & ) detaches cleanly and
        # the process is reparented to launchd so it survives.
        launch = ("( /bin/bash '%s' %s </dev/null >'%s' 2>&1 & )"
                  % (SCRIPT, cmd_args, log))

        if self.dry_run:
            subprocess.Popen(["/bin/bash", "-c", launch], start_new_session=True)
            launched = True
        else:
            launched = osascript_admin(launch)  # native password prompt

        # A successful launch attempt is not a running watchdog — the script
        # can die instantly (bad redirect, syntax error). Only claim Armed
        # once a monitor process actually appears; otherwise fail loud.
        armed = False
        if launched:
            for _ in range(8):
                if watchdog_pids():
                    armed = True
                    break
                time.sleep(0.25)

        if armed:
            notify("USB Watchdog", "Armed",
                   "Monitoring USB/Thunderbolt/SD/HDMI — instant shutdown%s."
                   % (" (dry-run)" if self.dry_run else ""))
        elif launched:
            tail = ""
            try:
                with open(log, encoding="utf-8", errors="replace") as f:
                    tail = f.read()[-500:].strip()
            except OSError:
                pass
            rumps.alert("Arm failed",
                        "The watchdog did not start.\n\nLog (%s):\n%s"
                        % (log, tail or "(no output)"))
        self.refresh(None)

    def disarm(self):
        pids = watchdog_pids()
        if not pids:
            return
        root = any(pid_is_root(p) for p in pids)

        # Use SIGKILL, not SIGTERM. When the watchdog is launched with
        # administrator privileges, it inherits SIGTERM set to ignored, so
        # bash's `trap` is a silent no-op and SIGTERM never stops it. SIGKILL
        # cannot be caught, blocked, or ignored.
        # Kill by explicit PID plus a pkill fallback; the [u]/[.] brackets stop
        # the command from matching its own helper shell. Errors on already-
        # dead PIDs are swallowed so the privileged call still returns success.
        pidlist = " ".join(str(p) for p in pids)
        kill_cmd = ("kill -9 %s 2>/dev/null; "
                    "pkill -9 -f '[u]sb_watchdog[.]sh' 2>/dev/null; true"
                    % pidlist)
        if root:
            osascript_admin(kill_cmd)  # native password prompt
        else:
            sh(["/bin/sh", "-c", kill_cmd])

        # SIGKILL is immediate, but children may take a beat to reap. Verify.
        for _ in range(12):
            if not watchdog_pids():
                break
            time.sleep(0.25)

        leftover = watchdog_pids()
        if leftover:
            # Fail loud — never leave the user thinking it disarmed when it
            # didn't (e.g. the password prompt was cancelled).
            rumps.alert(
                "Disarm did not complete",
                "The watchdog is still running (PID %s).\n\n"
                "If you cancelled the password prompt, click Disarm again "
                "and authenticate. To force-stop it from Terminal:\n\n"
                "    sudo pkill -9 -f usb_watchdog.sh"
                % ", ".join(str(p) for p in leftover))
        else:
            notify("USB Watchdog", "Disarmed", "Monitoring stopped.")
        self.refresh(None)

    def on_quit(self, _):
        if watchdog_pids():
            r = rumps.alert(
                "Quit the controller?",
                "The watchdog keeps running in the background after you quit "
                "this app (the menu-bar app is only a controller).\n\nDisarm "
                "first if you want to stop monitoring.",
                ok="Quit anyway", cancel="Cancel")
            if r != 1:
                return
        rumps.quit_application()

    # --- periodic UI refresh ---
    def refresh(self, _):
        pids = watchdog_pids()
        armed = bool(pids)
        rooted = armed and any(pid_is_root(p) for p in pids)
        dry = armed and not rooted

        if armed:
            self.title = ICON_DRYRUN if dry else ICON_ARMED
            self.status_item.title = ("● Armed%s — instant shutdown"
                                      % (" (dry-run)" if dry else ""))
            self.toggle_item.title = "Disarm"
            self._set_settings_enabled(False)
        else:
            self.title = ICON_DISARMED
            self.status_item.title = "● Disarmed"
            self.toggle_item.title = "Arm"
            self._set_settings_enabled(True)

        # Status above is a cheap ps read; the device submenu costs two
        # system_profiler runs, so refresh it on a slower cadence (~16s).
        if self._tick % 4 == 0:
            self._update_devices()
        self._tick += 1

    def _set_settings_enabled(self, enabled):
        # Dry-run choice is fixed while armed.
        self.dryrun_item.set_callback(self.on_toggle_dryrun if enabled else None)

    def _update_devices(self):
        _, out, _ = sh(["/bin/bash", SCRIPT, "--snapshot"])
        devs = [l for l in out.splitlines() if l.strip()]
        self.devices_menu.title = "Devices (%d)" % len(devs)
        self.devices_menu.clear()
        if devs:
            for d in devs:
                self.devices_menu.add(rumps.MenuItem(pretty_device(d)))
        else:
            none_item = rumps.MenuItem("(no peripherals connected)")
            none_item.set_callback(None)
            self.devices_menu.add(none_item)


if __name__ == "__main__":
    WatchdogApp().run()
