import os
from pathlib import Path
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

    def test_external_display_serial_is_included(self):
        fixture = """        Studio Display:\n          Display Serial Number: ABC123\n"""
        result = run_sourced(
            "printf %s " + shlex.quote(fixture) + " | parse_display_snapshot"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "DISPLAY:Studio Display serial=ABC123")


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
