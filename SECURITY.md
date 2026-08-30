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

## Explicit limitations

- USB location, vendor/product IDs, serial strings, Thunderbolt UIDs, display
  serials, and names are device- or OS-reported observations. They can be absent
  or spoofed. Two same-model devices without distinct reported identifiers may
  be indistinguishable.
- Changes can be missed if they happen and are reversed entirely between polls.
- macOS suspends the process during sleep. Default wake comparison cannot prove
  that nothing changed during sleep. Strict wake reduces that ambiguity by
  shutting down after every detected resume.
- Detection occurs after macOS enumerates a device. It cannot guarantee that a
  malicious peripheral has not already interacted with the OS.
- An administrator/root attacker, or an attacker able to modify the source/app
  before the user approves elevation, is outside the threat model.
- The detached engine is not automatically supervised or restarted. The menu
  app can alert on a stale/missing heartbeat only while the menu app is running.
- The graceful-shutdown fallback uses a quick halt and may cause data loss.
- Ad-hoc signing detects accidental post-build changes during local verification
  but supplies no publisher identity. The app is not notarized.

## Failure behavior

- **Inventory change, real mode:** normal shutdown is requested immediately;
  quick halt is the fallback after the configured grace period.
- **Persistent probe failure, real mode:** treated as a security fault and the
  same shutdown path begins.
- **Inventory change or probe failure, dry-run:** recorded and printed; no
  shutdown command is issued. A changed stable inventory becomes the new
  dry-run baseline so testing can continue.
- **Stale/malformed state:** shown as unhealthy when safely parseable, otherwise
  ignored; it never authorizes a broad process kill.
- **Engine crash:** no automatic restart. Its heartbeat becomes stale and the
  menu app reports a fault while running.

## Safer operation

Test every relevant device class in dry-run mode before arming real mode. Save
work before testing, keep the menu app open for health alerts, and disarm before
planned peripheral changes or sleep unless strict-wake shutdown is intended.

An unattended root LaunchDaemon is deliberately not included. A secure service
would require a root-owned, non-user-writable installed engine, an explicit
install/uninstall lifecycle, and carefully defined restart-versus-disarm
semantics.
