# Security model

## Intended property

After a complete stable baseline has been established, a successfully observed
change in the USB, Thunderbolt, SD-card, or external-display inventory causes
the real-mode engine to begin shutdown. Persistent loss of an inventory probe
also fails closed in real mode.

This is a local tamper alarm. It is not a cryptographic peripheral identity
system and does not claim to prevent or contain a malicious device.

## Trust boundaries and invariants

- The repository or built app must be trusted before approving its
  administrator prompt. Real mode executes the bundled/current shell engine as
  root for that arm session.
- A baseline is accepted only after all four probes succeed and both fast and
  slow inventory groups return the same result twice.
- Probe errors and timeouts are distinct from a successful empty inventory.
- The native IOKit listener supplies scheduling hints only. Every notification
  still passes through the shell engine's bounded inventory probe, baseline
  comparison, and existing shutdown-policy path.
- In real mode, the root shell launches the separate, mutable listener at the
  requesting user's UID. Replacing that helper cannot create a new path to root
  code execution; false or missing hints cannot disable independent polling.
- The listener emits a heartbeat. Exit, malformed output, or a missing heartbeat
  degrades visibly to the independently timed polling fallback.
- The menu app reports Armed only when the exact registered PID, UID, launch
  token, process start identity, ready state, and recent heartbeat all match.
- State files must be regular, correctly owned, non-group-writable, and
  non-world-writable. The root instance uses `/var/run/usb-watchdog.state`; the
  dry-run instance uses the current user's Application Support directory.
- Disarm never scans or kills by process-name pattern. It validates the exact
  registration and writes a token/start-bound stop request; the matching engine
  exits and cleans up its own state.
- A future-dated or stale heartbeat is unhealthy. Process start identities are
  captured and compared in a stable UTC/C locale.
- The shell engine accepts only the two documented shutdown-policy values. Both
  inventory changes and persistent probe failures reach the same policy-aware
  shutdown function.
- Logs can contain observed device names and identifiers. The menu app creates
  or repairs both log files as owner-readable only and refuses symlink or
  non-file log targets. The root-readable state contains only a generic event
  summary rather than the device-bearing shutdown reason.

## Explicit limitations

- USB location, vendor/product IDs, serial and product strings, revision values,
  device class, configuration count, maximum control-packet size, and published
  interface profiles are device- or OS-reported observations. They can be absent
  or spoofed. A device that reproduces the complete observed profile may remain
  indistinguishable.
- IOKit device and interface notifications reduce the ordinary delay before a
  USB check but do not make detection instantaneous. A publish or termination
  notification is only a prompt to collect a fresh snapshot. An attach/remove
  sequence completed before that snapshot can still be missed if the final
  inventory matches the baseline.
- macOS suspends the process during sleep. After wake, the engine compares a new
  stable snapshot with its pre-sleep baseline and shuts down on a difference.
  A device attached and removed entirely while the Mac remained asleep leaves no
  final inventory difference to detect.
- Detection occurs after macOS begins enumerating and publishing a device. It
  cannot guarantee that a malicious peripheral has not already interacted with
  the OS.
- An administrator/root attacker, or an attacker able to modify the source/app
  before the user approves elevation, is outside the threat model.
- The detached engine is not automatically supervised or restarted. The menu
  app can alert on a stale/missing heartbeat only while the menu app is running.
- The default response attempts a graceful shutdown before using a forced quick
  halt. The optional immediate response skips the graceful attempt. Either can
  cause data loss.
- Ad-hoc signing detects accidental post-build changes during local verification
  but supplies no publisher identity. The app is not notarized.

## Failure behavior

- **Inventory change, real mode:** the selected response either requests normal
  shutdown before the forced-halt fallback or begins forced halt immediately.
- **Persistent probe failure, real mode:** treated as a security fault and the
  same shutdown path begins.
- **Inventory change or probe failure, dry-run:** recorded and printed; no
  shutdown command is issued. A changed stable inventory becomes the new
  dry-run baseline so testing can continue.
- **Stale/malformed state:** shown as unhealthy when safely parseable, otherwise
  ignored; it never authorizes a broad process kill.
- **Engine crash:** no automatic restart. Its heartbeat becomes stale and the
  menu app reports a fault while running.
- **Native event-listener failure:** the menu reports polling fallback, while the
  shell engine continues its independently timed USB and Thunderbolt checks.

## Safer operation

Test every relevant device class in dry-run mode before arming real mode. Save
work before testing, keep the menu app open for health alerts, and disarm before
planned peripheral changes.

On a Mac laptop with Apple silicon, use macOS accessory security as the primary
preventive control. Setting **Allow accessories to connect** to **Always Ask**
requires approval before an accessory receives data access. USB Watchdog remains
a later detection-and-response layer and is not a replacement for that control.

An unattended root LaunchDaemon is deliberately not included. A secure service
would require a root-owned, non-user-writable installed engine, an explicit
install/uninstall lifecycle, and carefully defined restart-versus-disarm
semantics.
