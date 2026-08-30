# USB Watchdog

USB Watchdog is a macOS menu-bar tamper alarm. It records the observable USB,
Thunderbolt, SD-card, and external-display inventory, then initiates shutdown if
that inventory changes.

It is intentionally conservative, but it is **not device authentication**. A
device that reproduces the same descriptors can be indistinguishable, and a
change completed and reversed between polls—or while the Mac is asleep—can be
missed. Read [SECURITY.md](SECURITY.md) before relying on it.

## Install and run

Python virtual environments contain absolute paths, so recreate `.venv` after
moving or renaming this folder:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Then either double-click `USB Watchdog.command`, or build and open the local app:

```sh
./scripts/build_app.sh
open "dist/USB Watchdog.app"
```

Start with **Dry-run** selected. Arm the watchdog, add or remove each kind of
device you care about, and confirm the change is reported without a shutdown.

Real mode asks for an administrator password each time it is armed. A healthy
green status means a complete, stable baseline was established and both probe
groups are still succeeding. Orange means the registered process is stale,
faulted, or no longer reporting a current heartbeat; PID presence alone is not
treated as healthy.

## Behavior

- USB and Thunderbolt are checked on a roughly 0.25-second loop, plus probe
  execution time.
- SD cards and external displays are checked every 12 fast loops, roughly every
  3 seconds plus probe execution time.
- Every inventory command has a 3-second timeout and bounded retries.
- In real mode, a persistent inventory-probe failure fails closed by initiating
  shutdown. In dry-run mode, it reports a fault and retains the last known-good
  baseline until that probe group recovers.
- After wake, the engine waits briefly for hardware to settle and compares a new
  stable snapshot with the pre-sleep baseline. A remaining difference initiates
  shutdown.
- Shutdown first requests the normal syncing macOS shutdown. If the machine is
  still running after 5 seconds, the engine repeatedly requests a quick halt,
  which can lose unsaved data.

The menu app starts a detached instance, but there is no automatic restart
service. If the engine exits, the open menu app reports the stale/missing
heartbeat. Quitting the menu app does not stop an armed engine; disarm it first
if monitoring should end.

The menu app controls only the two exact instances it registered. A watchdog
started directly in Terminal without a state file remains independent.

## Command-line checks

Print the current validated snapshot without arming:

```sh
./usb_watchdog.sh --snapshot
```

Run without shutdown capability:

```sh
./usb_watchdog.sh --dry-run
```

Run for real in the foreground:

```sh
sudo ./usb_watchdog.sh
```

## Development

Run the regression suite and static checks:

```sh
.venv/bin/python -m unittest discover -s tests -v
/bin/bash -n usb_watchdog.sh
shellcheck usb_watchdog.sh
```

`requirements-build.txt` records the complete dependency set used for the
current local app build. `scripts/build_app.sh` removes only the ignored
`build/` and `dist/` outputs, builds one bundle, removes unsupported extended
attributes, applies an ad-hoc local signature, verifies that signature, and
checks that the bundled shell engine exactly matches the source tree.

The resulting app is for local use. It is not Developer ID signed or notarized
for distribution.
