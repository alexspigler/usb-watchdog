import os
from pathlib import Path
import select
import shlex
import subprocess
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "usb_watchdog.sh"


def run_sourced(body, timeout=10):
    command = f"source {shlex.quote(str(SCRIPT))}\n{body}"
    return subprocess.run(
        ["/bin/bash", "-c", command],
        text=True,
        capture_output=True,
        timeout=timeout,
    )


class ProbeContractTests(unittest.TestCase):
    def test_removed_strict_wake_option_is_rejected(self):
        result = subprocess.run(
            ["/bin/bash", str(SCRIPT), "--wake-policy", "shutdown", "--snapshot"],
            text=True,
            capture_output=True,
            timeout=3,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown option", result.stderr)

    def test_successful_empty_fast_snapshot_is_valid(self):
        result = run_sourced(
            "get_usb_snapshot() { return 0; }\n"
            "get_thunderbolt_snapshot() { return 0; }\n"
            "get_fast_snapshot"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_partial_probe_failure_is_not_coerced_to_empty_success(self):
        result = run_sourced(
            "get_usb_snapshot() { return 1; }\n"
            "get_thunderbolt_snapshot() { return 0; }\n"
            "get_fast_snapshot"
        )
        self.assertNotEqual(result.returncode, 0)

    def test_total_probe_failure_is_not_coerced_to_empty_success(self):
        result = run_sourced(
            "run_with_timeout() { return 1; }\n"
            "get_fast_snapshot"
        )
        self.assertNotEqual(result.returncode, 0)

    def test_stable_snapshot_requires_success(self):
        result = run_sourced(
            "STABLE_ATTEMPTS=2\n"
            "get_fast_snapshot() { return 1; }\n"
            "collect_stable_snapshot fast"
        )
        self.assertNotEqual(result.returncode, 0)

    def test_stable_empty_snapshot_is_accepted(self):
        result = run_sourced(
            "get_fast_snapshot() { return 0; }\n"
            "collect_stable_snapshot fast"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_probe_timeout_is_bounded(self):
        started = time.monotonic()
        result = run_sourced("run_with_timeout 1 /bin/sleep 3", timeout=3)
        elapsed = time.monotonic() - started
        self.assertNotEqual(result.returncode, 0)
        self.assertLess(elapsed, 2.5)

    def test_shutdown_policy_defaults_to_graceful_then_force(self):
        result = run_sourced("parse_args\nprintf %s \"$SHUTDOWN_POLICY\"")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "graceful-then-force")

    def test_both_shutdown_policies_are_accepted(self):
        for policy in ("graceful-then-force", "force-immediately"):
            with self.subTest(policy=policy):
                result = run_sourced(
                    "parse_args --shutdown-policy %s\nprintf %%s \"$SHUTDOWN_POLICY\""
                    % shlex.quote(policy)
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, policy)

    def test_invalid_shutdown_policy_is_rejected(self):
        result = subprocess.run(
            [
                "/bin/bash",
                str(SCRIPT),
                "--shutdown-policy",
                "power-off-maybe",
                "--snapshot",
            ],
            text=True,
            capture_output=True,
            timeout=3,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--shutdown-policy must be", result.stderr)

    def test_missing_shutdown_policy_value_is_rejected(self):
        result = subprocess.run(
            ["/bin/bash", str(SCRIPT), "--shutdown-policy"],
            text=True,
            capture_output=True,
            timeout=3,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("requires a policy", result.stderr)


class ParserTests(unittest.TestCase):
    def test_usb_without_serial_has_explicit_empty_serial_field(self):
        fixture = r'''
+-o Keyboard@00100000  <class IOUSBHostDevice, id 1>
  "idVendor" = 1234
  "idProduct" = 5678
  "locationID" = 1048576
  "USB Product Name" = "Keyboard"
'''
        result = run_sourced(
            "printf %s " + shlex.quote(fixture) + " | parse_usb_snapshot"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("USB:port=1048576 1234:5678 sn= Keyboard", result.stdout)
        self.assertIn(
            "profile=usb=?;rev=?;device=?/?/?;packet=?;configs=?;interfaces=none",
            result.stdout,
        )

    def test_usb_profile_includes_static_device_and_interface_descriptors(self):
        fixture = r'''
+-o Composite Device@00100000  <class IOUSBHostDevice, id 1>
  "idVendor" = 1234
  "idProduct" = 5678
  "locationID" = 1048576
  "bcdUSB" = 512
  "bcdDevice" = 257
  "bDeviceClass" = 0
  "bDeviceSubClass" = 0
  "bDeviceProtocol" = 0
  "bMaxPacketSize0" = 64
  "bNumConfigurations" = 1
  "kUSBSerialNumberString" = "SERIAL1"
  "kUSBProductString" = "Composite Device"
  +-o Storage Interface  <class IOUSBHostInterface, id 2>
    "bConfigurationValue" = 1
    "bInterfaceNumber" = 1
    "bInterfaceClass" = 8
    "bInterfaceSubClass" = 6
    "bInterfaceProtocol" = 80
    "bNumEndpoints" = 2
  +-o Keyboard Interface  <class IOUSBHostInterface, id 3>
    "bConfigurationValue" = 1
    "bInterfaceNumber" = 0
    "bInterfaceClass" = 3
    "bInterfaceSubClass" = 1
    "bInterfaceProtocol" = 1
    "bNumEndpoints" = 1
'''
        result = run_sourced(
            "printf %s " + shlex.quote(fixture) + " | parse_usb_snapshot"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "USB:port=1048576 1234:5678 sn=SERIAL1 Composite Device | "
            "profile=usb=512;rev=257;device=0/0/0;packet=64;configs=1;"
            "interfaces=0:3/1/1,1:8/6/80",
        )

    def test_interface_order_does_not_change_the_usb_fingerprint(self):
        header = r'''
+-o Composite Device@00100000  <class IOUSBHostDevice, id 1>
  "idVendor" = 1234
  "idProduct" = 5678
  "locationID" = 1048576
  "USB Product Name" = "Composite Device"
'''
        keyboard = r'''
  +-o Keyboard Interface  <class IOUSBHostInterface, id 2>
    "bConfigurationValue" = 1
    "bInterfaceNumber" = 0
    "bInterfaceClass" = 3
    "bInterfaceSubClass" = 1
    "bInterfaceProtocol" = 1
    "bNumEndpoints" = 1
'''
        storage = r'''
  +-o Storage Interface  <class IOUSBHostInterface, id 3>
    "bConfigurationValue" = 1
    "bInterfaceNumber" = 1
    "bInterfaceClass" = 8
    "bInterfaceSubClass" = 6
    "bInterfaceProtocol" = 80
    "bNumEndpoints" = 2
'''
        first = run_sourced(
            "printf %s "
            + shlex.quote(header + keyboard + storage)
            + " | parse_usb_snapshot"
        )
        second = run_sourced(
            "printf %s "
            + shlex.quote(header + storage + keyboard)
            + " | parse_usb_snapshot"
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout, second.stdout)

    def test_new_interface_changes_fingerprint_even_when_device_ids_match(self):
        base = r'''
+-o Composite Device@00100000  <class IOUSBHostDevice, id 1>
  "idVendor" = 1234
  "idProduct" = 5678
  "locationID" = 1048576
  "USB Serial Number" = "SERIAL1"
  "USB Product Name" = "Composite Device"
  +-o Storage Interface  <class IOUSBHostInterface, id 2>
    "bConfigurationValue" = 1
    "bInterfaceNumber" = 0
    "bInterfaceClass" = 8
    "bInterfaceSubClass" = 6
    "bInterfaceProtocol" = 80
    "bNumEndpoints" = 2
'''
        keyboard = r'''
  +-o Keyboard Interface  <class IOUSBHostInterface, id 3>
    "bConfigurationValue" = 1
    "bInterfaceNumber" = 1
    "bInterfaceClass" = 3
    "bInterfaceSubClass" = 1
    "bInterfaceProtocol" = 1
    "bNumEndpoints" = 1
'''
        base_result = run_sourced(
            "printf %s " + shlex.quote(base) + " | parse_usb_snapshot"
        )
        changed_result = run_sourced(
            "printf %s " + shlex.quote(base + keyboard) + " | parse_usb_snapshot"
        )
        self.assertEqual(base_result.returncode, 0, base_result.stderr)
        self.assertEqual(changed_result.returncode, 0, changed_result.stderr)
        self.assertNotEqual(base_result.stdout, changed_result.stdout)
        self.assertIn("1:3/1/1", changed_result.stdout)

    def test_active_configuration_and_endpoint_count_are_not_fingerprinted(self):
        template = r'''
+-o Audio Device@00100000  <class IOUSBHostDevice, id 1>
  "idVendor" = 1234
  "idProduct" = 5678
  "locationID" = 1048576
  "USB Product Name" = "Audio Device"
  +-o Audio Interface  <class IOUSBHostInterface, id 2>
    "bConfigurationValue" = %s
    "bInterfaceNumber" = 1
    "bInterfaceClass" = 1
    "bInterfaceSubClass" = 2
    "bInterfaceProtocol" = 0
    "bNumEndpoints" = %s
'''
        first = run_sourced(
            "printf %s " + shlex.quote(template % (1, 0)) + " | parse_usb_snapshot"
        )
        second = run_sourced(
            "printf %s " + shlex.quote(template % (2, 3)) + " | parse_usb_snapshot"
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(first.stdout, second.stdout)

    def test_external_display_serial_is_included(self):
        fixture = """        Studio Display:\n          Display Serial Number: ABC123\n"""
        result = run_sourced(
            "printf %s " + shlex.quote(fixture) + " | parse_display_snapshot"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "DISPLAY:Studio Display serial=ABC123")

    def test_thunderbolt_uid_vendor_and_device_are_included(self):
        fixture = r'''
+-o Thunderbolt Dock@0  <class IOThunderboltPort, id 1>
  "Vendor Name" = "CalDigit"
  "Device Name" = "TS4"
  "UID" = "0x00ABCDEF12345678"
'''
        result = run_sourced(
            "printf %s " + shlex.quote(fixture) + " | parse_thunderbolt_snapshot"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(),
            "TB:uid=0x00ABCDEF12345678 CalDigit / TS4",
        )

    def test_internal_sd_reader_without_media_is_ignored(self):
        fixture = """Card Reader:\n    Built in SD Card Reader:\n      Vendor ID: 0x1234\n      Device ID: 0x5678\n      Link Speed: 2.5 GT/s\n"""
        result = run_sourced(
            "printf %s " + shlex.quote(fixture) + " | parse_sd_snapshot"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_inserted_sd_media_name_is_included(self):
        fixture = """Card Reader:\n    Built in SD Card Reader:\n      Vendor ID: 0x1234\n        SDXC Card:\n"""
        result = run_sourced(
            "printf %s " + shlex.quote(fixture) + " | parse_sd_snapshot"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "SD:SDXC Card")


class StopBoundaryTests(unittest.TestCase):
    def test_stale_exact_record_is_removed_without_pattern_kill(self):
        token = "0123456789abcdef0123456789abcdef"
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "watchdog.state"
            state_path.write_text(
                "pid=99999\nuid=501\ntoken=%s\nstarted=never\n" % token,
                encoding="utf-8",
            )
            lock = Path(str(state_path) + ".lock")
            lock.mkdir()
            result = run_sourced(
                "STATE_FILE=%s\nINSTANCE_TOKEN=%s\n"
                "process_matches_instance() { return 1; }\n"
                "stop_registered_instance"
                % (shlex.quote(str(state_path)), shlex.quote(token))
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(state_path.exists())
            self.assertFalse(lock.exists())

    def test_live_exact_record_receives_cooperative_stop_request(self):
        token = "0123456789abcdef0123456789abcdef"
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "watchdog.state"
            state_path.write_text(
                "pid=4321\nuid=%s\ntoken=%s\nstarted=known-start\n"
                % (os.getuid(), token),
                encoding="utf-8",
            )
            result = run_sourced(
                "STATE_FILE=%s\nINSTANCE_TOKEN=%s\n"
                "process_matches_instance() { return 0; }\n"
                "wait_for_instance_stop() { return 0; }\n"
                "stop_registered_instance"
                % (shlex.quote(str(state_path)), shlex.quote(token))
            )
            request = Path(str(state_path) + ".stop")
            self.assertEqual(result.returncode, 0, result.stderr)
            request_text = request.read_text(encoding="utf-8")
            self.assertIn("token=%s" % token, request_text)
            self.assertIn("started=known-start", request_text)

    def test_no_pattern_kill_or_full_process_scan_remains(self):
        shell_source = SCRIPT.read_text(encoding="utf-8")
        gui_source = (ROOT / "usb_watchdog_gui.py").read_text(encoding="utf-8")
        combined = shell_source + gui_source
        self.assertNotIn("pkill", combined)
        self.assertNotIn("/bin/kill -9", combined)
        self.assertNotIn("ps\", \"-axo", combined)


class HealthStateTests(unittest.TestCase):
    def test_fast_heartbeat_cannot_clear_slow_probe_fault(self):
        token = "0123456789abcdef0123456789abcdef"
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "watchdog.state"
            result = run_sourced(
                "STATE_FILE=%s\nINSTANCE_TOKEN=%s\nINSTANCE_STARTED=known-start\n"
                "FAST_HEALTHY=true\nSLOW_HEALTHY=false\nLAST_HEARTBEAT=0\n"
                "write_heartbeat"
                % (shlex.quote(str(state_path)), shlex.quote(token))
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("status=fault", state_path.read_text(encoding="utf-8"))

    def test_process_identity_uses_stable_utc_c_locale(self):
        shell_source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("TZ=UTC LC_ALL=C /bin/ps -p $$ -o lstart=", shell_source)


class RuntimeChangeTests(unittest.TestCase):
    def run_monitor_cycle(
        self,
        *,
        fast_initial="BASE",
        slow_initial="BASE",
        confirmation="BASE",
        slow_cycles=99,
        confirmation_fails=False,
    ):
        if confirmation_fails:
            confirmation_body = "return 1"
        else:
            confirmation_body = "printf %s " + shlex.quote(confirmation)
        body = (
            "FAST_INTERVAL=0\n"
            "WAKE_GAP_SECONDS=999999\n"
            "PROBE_RETRIES=1\n"
            f"SLOW_CYCLES={slow_cycles}\n"
            "FAST_BASE=BASE\n"
            "SLOW_BASE=BASE\n"
            "stop_calls=0\n"
            "stop_requested() {\n"
            "  stop_calls=$((stop_calls + 1))\n"
            "  (( stop_calls > 1 ))\n"
            "}\n"
            "write_heartbeat() { :; }\n"
            "write_state() { :; }\n"
            "get_fast_snapshot() { printf %s "
            + shlex.quote(fast_initial)
            + "; }\n"
            "get_slow_snapshot() { printf %s "
            + shlex.quote(slow_initial)
            + "; }\n"
            "snapshot_getter() { "
            + confirmation_body
            + "; }\n"
            "process_change() {\n"
            "  printf 'EVENT:%s->%s\\n' \"$1\" \"$2\"\n"
            "  return 1\n"
            "}\n"
            "runtime_probe_fault() {\n"
            "  printf 'FAULT:%s\\n' \"$1\"\n"
            "  return 1\n"
            "}\n"
            "monitor_loop\n"
            "printf 'FAST_BASE:%s\\n' \"$FAST_BASE\"\n"
            "printf 'SLOW_BASE:%s\\n' \"$SLOW_BASE\""
        )
        return run_sourced(body, timeout=3)

    def test_fast_change_is_enforced_before_reverting_confirmation(self):
        result = self.run_monitor_cycle(fast_initial="CHANGED")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("EVENT:BASE->CHANGED"), 1)
        self.assertIn("FAST_BASE:BASE", result.stdout)

    def test_slow_change_is_enforced_before_reverting_confirmation(self):
        result = self.run_monitor_cycle(slow_initial="CHANGED", slow_cycles=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("EVENT:BASE->CHANGED"), 1)
        self.assertIn("SLOW_BASE:BASE", result.stdout)

    def test_persistent_dry_run_change_updates_baseline_after_event(self):
        result = self.run_monitor_cycle(
            fast_initial="CHANGED", confirmation="CHANGED"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("EVENT:BASE->CHANGED"), 1)
        self.assertIn("FAST_BASE:CHANGED", result.stdout)

    def test_confirmation_failure_keeps_baseline_after_event(self):
        result = self.run_monitor_cycle(
            fast_initial="CHANGED", confirmation_fails=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("EVENT:BASE->CHANGED"), 1)
        self.assertIn("FAULT:USB/Thunderbolt confirmation", result.stdout)
        self.assertIn("FAST_BASE:BASE", result.stdout)


class NativeEventMonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.directory.name) / "usb_watchdog_event_monitor"
        result = subprocess.run(
            [
                "/usr/bin/clang",
                "-std=c11",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-framework",
                "CoreFoundation",
                "-framework",
                "IOKit",
                str(ROOT / "usb_watchdog_events.c"),
                "-o",
                str(cls.binary),
            ],
            text=True,
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise AssertionError(result.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_invalid_mode_is_rejected(self):
        result = subprocess.run(
            [str(self.binary), "--snapshot"],
            text=True,
            capture_output=True,
            timeout=3,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--events", result.stderr)

    def test_listener_registers_and_reports_heartbeats(self):
        process = subprocess.Popen(
            [str(self.binary), "--events"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            readable, _, _ = select.select([process.stdout], [], [], 3)
            self.assertTrue(readable, "event monitor did not report readiness")
            self.assertEqual(process.stdout.readline().strip(), "ready")
            readable, _, _ = select.select([process.stdout], [], [], 3)
            self.assertTrue(readable, "event monitor heartbeat was not received")
            self.assertEqual(process.stdout.readline().strip(), "heartbeat")
        finally:
            process.terminate()
            process.wait(timeout=3)
            process.stdout.close()
            process.stderr.close()

    def test_listener_watches_device_and_interface_services(self):
        source = (ROOT / "usb_watchdog_events.c").read_text(encoding="utf-8")
        self.assertIn('"IOUSBHostDevice"', source)
        self.assertIn('"IOUSBHostInterface"', source)


class EventFallbackTests(unittest.TestCase):
    def test_partial_helper_line_cannot_block_polling_fallback(self):
        started = time.monotonic()
        result = run_sourced(
            "exec 9< <(exec /usr/bin/perl -e '$|=1; print \"partial-message\"; sleep 30')\n"
            "helper_pid=$!\n"
            "set +e\n"
            "read_event_with_timeout 0.1\n"
            "status=$?\n"
            "exec 9<&-\n"
            "/bin/kill \"$helper_pid\" 2>/dev/null || true\n"
            "wait \"$helper_pid\" 2>/dev/null || true\n"
            "printf %s \"$status\"",
            timeout=2,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "2")
        self.assertLess(elapsed, 1.0)

    def test_helper_that_ignores_term_is_force_stopped_within_bound(self):
        started = time.monotonic()
        result = run_sourced(
            "exec 8< <(exec /usr/bin/perl -e '$SIG{TERM}=sub {}; $|=1; print \"ready\\n\"; while (1) { sleep 30 }')\n"
            "helper_pid=$!\n"
            "IFS= read -r -u 8 ready\n"
            "[[ \"$ready\" == ready ]]\n"
            "EVENT_MONITOR_PID=$helper_pid\n"
            "EVENT_MONITOR_FD_OPEN=false\n"
            "stop_usb_event_monitor\n"
            "exec 8<&-\n"
            "printf stopped",
            timeout=2,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "stopped")
        self.assertLess(elapsed, 1.5)

    def test_event_hint_wakes_without_waiting_for_poll_timeout(self):
        started = time.monotonic()
        result = run_sourced(
            "FAST_INTERVAL=1\n"
            "EVENT_MONITOR_ACTIVE=true\n"
            "EVENT_MONITOR_HEALTHY=true\n"
            "EVENT_LAST_HEARTBEAT=$SECONDS\n"
            "read_event_with_timeout() { printf usb-published; }\n"
            "wait_for_fast_check\n"
            "printf %s \"$EVENT_MONITOR_HEALTHY\""
        )
        elapsed = time.monotonic() - started
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "true")
        self.assertLess(elapsed, 0.5)

    def test_helper_timeout_keeps_polling_path_healthy(self):
        result = run_sourced(
            "EVENT_MONITOR_ACTIVE=true\n"
            "EVENT_MONITOR_HEALTHY=true\n"
            "EVENT_MONITOR_PID=$$\n"
            "EVENT_LAST_HEARTBEAT=$SECONDS\n"
            "read_event_with_timeout() { return 1; }\n"
            "wait_for_fast_check\n"
            "printf '%s:%s' \"$EVENT_MONITOR_ACTIVE\" \"$EVENT_MONITOR_HEALTHY\""
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "true:true")

    def test_helper_eof_degrades_to_polling(self):
        result = run_sourced(
            "EVENT_MONITOR_ACTIVE=true\n"
            "EVENT_MONITOR_HEALTHY=true\n"
            "EVENT_MONITOR_PID=\n"
            "EVENT_MONITOR_FD_OPEN=false\n"
            "FAST_HEALTHY=true\n"
            "SLOW_HEALTHY=true\n"
            "read_event_with_timeout() { return 2; }\n"
            "write_state() { printf 'STATE:%s:%s\\n' \"$1\" \"$2\"; }\n"
            "wait_for_fast_check\n"
            "printf 'MODE:%s:%s' \"$EVENT_MONITOR_ACTIVE\" \"$EVENT_MONITOR_HEALTHY\""
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("STATE:polling:", result.stdout)
        self.assertIn("MODE:false:false", result.stdout)

    def test_invalid_helper_message_degrades_to_polling(self):
        result = run_sourced(
            "EVENT_MONITOR_ACTIVE=true\n"
            "EVENT_MONITOR_HEALTHY=true\n"
            "EVENT_MONITOR_PID=\n"
            "EVENT_MONITOR_FD_OPEN=false\n"
            "FAST_HEALTHY=true\n"
            "SLOW_HEALTHY=true\n"
            "read_event_with_timeout() { printf unexpected-message; }\n"
            "write_state() { printf 'STATE:%s:%s\\n' \"$1\" \"$2\"; }\n"
            "wait_for_fast_check\n"
            "printf 'MODE:%s:%s' \"$EVENT_MONITOR_ACTIVE\" \"$EVENT_MONITOR_HEALTHY\""
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("STATE:polling:", result.stdout)
        self.assertIn("MODE:false:false", result.stdout)

    def test_helper_loss_cannot_mask_existing_probe_fault(self):
        result = run_sourced(
            "EVENT_MONITOR_ACTIVE=true\n"
            "EVENT_MONITOR_HEALTHY=true\n"
            "EVENT_MONITOR_PID=\n"
            "EVENT_MONITOR_FD_OPEN=false\n"
            "FAST_HEALTHY=false\n"
            "SLOW_HEALTHY=true\n"
            "read_event_with_timeout() { return 2; }\n"
            "write_state() { printf 'STATE:%s:%s\\n' \"$1\" \"$2\"; }\n"
            "wait_for_fast_check"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("STATE:fault:", result.stdout)
        self.assertNotIn("STATE:polling:", result.stdout)

    def test_stale_helper_heartbeat_degrades_even_when_pipe_stays_open(self):
        result = run_sourced(
            "/bin/sleep 10 &\n"
            "EVENT_MONITOR_ACTIVE=true\n"
            "EVENT_MONITOR_HEALTHY=true\n"
            "EVENT_MONITOR_PID=$!\n"
            "EVENT_MONITOR_FD_OPEN=false\n"
            "FAST_HEALTHY=true\n"
            "SLOW_HEALTHY=true\n"
            "EVENT_LAST_HEARTBEAT=0\n"
            "EVENT_HEARTBEAT_TIMEOUT_SECONDS=0\n"
            "SECONDS=2\n"
            "read_event_with_timeout() { return 1; }\n"
            "write_state() { printf 'STATE:%s:%s\\n' \"$1\" \"$2\"; }\n"
            "wait_for_fast_check\n"
            "printf 'MODE:%s:%s' \"$EVENT_MONITOR_ACTIVE\" \"$EVENT_MONITOR_HEALTHY\""
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("STATE:polling:", result.stdout)
        self.assertIn("MODE:false:false", result.stdout)

class ShutdownPolicyTests(unittest.TestCase):
    def run_shutdown(self, policy, dry_run=False):
        return run_sourced(
            "SHUTDOWN_POLICY=%s\n"
            "DRY_RUN=%s\n"
            "write_state() { printf 'STATE:%%s:%%s\\n' \"$1\" \"$2\"; }\n"
            "request_graceful_shutdown() { echo GRACEFUL; }\n"
            "wait_before_forced_halt() { echo WAIT; }\n"
            "force_halt_forever() { echo FORCED; }\n"
            "do_shutdown 'REMOVED: serial=private' 'hardware inventory change detected'"
            % (shlex.quote(policy), "true" if dry_run else "false")
        )

    def test_graceful_policy_requests_normal_shutdown_before_forced_halt(self):
        result = self.run_shutdown("graceful-then-force")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(result.stdout.index("GRACEFUL"), result.stdout.index("FORCED"))

    def test_immediate_policy_skips_graceful_shutdown(self):
        result = self.run_shutdown("force-immediately")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("GRACEFUL", result.stdout)
        self.assertIn("FORCED", result.stdout)

    def test_dry_run_calls_neither_shutdown_path(self):
        for policy in ("graceful-then-force", "force-immediately"):
            with self.subTest(policy=policy):
                result = self.run_shutdown(policy, dry_run=True)
                self.assertNotIn("GRACEFUL", result.stdout)
                self.assertNotIn("FORCED", result.stdout)
                self.assertIn("Policy: %s" % policy, result.stdout)

    def test_state_detail_excludes_device_identifiers(self):
        result = self.run_shutdown("force-immediately")
        state_line = next(
            line for line in result.stdout.splitlines() if line.startswith("STATE:")
        )
        self.assertEqual(
            state_line, "STATE:shutting-down:hardware inventory change detected"
        )
        self.assertNotIn("serial=private", state_line)

    def test_probe_fault_uses_the_selected_common_shutdown_sink(self):
        result = run_sourced(
            "DRY_RUN=false\n"
            "SHUTDOWN_POLICY=force-immediately\n"
            "write_state() { :; }\n"
            "do_shutdown() { printf 'SINK:%s:%s:%s\\n' \"$SHUTDOWN_POLICY\" \"$1\" \"$2\"; }\n"
            "runtime_probe_fault Display"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SINK:force-immediately:PROBE FAULT: Display", result.stdout)
        self.assertIn("hardware inventory probe failure", result.stdout)

    def test_inventory_change_uses_the_selected_common_shutdown_sink(self):
        result = run_sourced(
            "SHUTDOWN_POLICY=graceful-then-force\n"
            "do_shutdown() { printf 'SINK:%s:%s:%s\\n' \"$SHUTDOWN_POLICY\" \"$1\" \"$2\"; }\n"
            "process_change 'USB:old' 'USB:new'"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SINK:graceful-then-force:", result.stdout)
        self.assertIn("hardware inventory change detected", result.stdout)


class LiveSnapshotTests(unittest.TestCase):
    def test_snapshot_mode_returns_a_valid_result(self):
        result = subprocess.run(
            ["/bin/bash", str(SCRIPT), "--snapshot"],
            text=True,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
