import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]


def load_gui_module():
    try:
        import rumps  # noqa: F401
    except ImportError:
        fake = types.ModuleType("rumps")
        fake.App = object
        fake.MenuItem = object
        fake.Timer = object
        fake.notification = lambda *args, **kwargs: None
        fake.alert = lambda *args, **kwargs: 0
        fake.quit_application = lambda: None
        sys.modules["rumps"] = fake

    spec = importlib.util.spec_from_file_location(
        "usb_watchdog_gui_under_test", ROOT / "usb_watchdog_gui.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gui = load_gui_module()


class StateTests(unittest.TestCase):
    TOKEN = "0123456789abcdef0123456789abcdef"

    def write_state(self, directory, **overrides):
        values = {
            "version": "1",
            "pid": "4321",
            "uid": str(os.getuid()),
            "token": self.TOKEN,
            "started": "Sun Aug 30 18:20:31 2026",
            "mode": "dry-run",
            "shutdown_policy": gui.GRACEFUL_THEN_FORCE,
            "status": "ready",
            "heartbeat": "1000",
            "detail": "monitoring",
        }
        values.update({key: str(value) for key, value in overrides.items()})
        state_path = Path(directory) / "watchdog.state"
        state_path.write_text(
            "".join(f"{key}={value}\n" for key, value in values.items()),
            encoding="utf-8",
        )
        return state_path

    def test_exact_token_and_uid_make_ready_state_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = self.write_state(directory)
            process = (
                f"{os.getuid()} /bin/bash /tmp/usb_watchdog.sh "
                f"--instance-token {self.TOKEN} --dry-run\n"
            )
            with mock.patch.object(
                gui,
                "sh",
                side_effect=[
                    (0, process, ""),
                    (0, "Sun Aug 30 18:20:31 2026\n", ""),
                ],
            ):
                state = gui.read_instance(str(state_path), now=1005)
        self.assertTrue(state["alive"])
        self.assertTrue(state["healthy"])

    def test_editor_with_script_name_is_not_the_registered_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = self.write_state(directory)
            editor = f"{os.getuid()} vim usb_watchdog.sh\n"
            with mock.patch.object(gui, "sh", return_value=(0, editor, "")):
                state = gui.read_instance(str(state_path), now=1005)
        self.assertFalse(state["alive"])
        self.assertFalse(state["healthy"])

    def test_wrong_uid_or_token_is_not_alive(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = self.write_state(directory)
            wrong = (
                f"{os.getuid() + 1} /bin/bash /tmp/usb_watchdog.sh "
                "--instance-token ffffffffffffffffffffffffffffffff\n"
            )
            with mock.patch.object(gui, "sh", return_value=(0, wrong, "")):
                state = gui.read_instance(str(state_path), now=1005)
        self.assertFalse(state["alive"])

    def test_stale_heartbeat_is_visible_but_not_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = self.write_state(directory)
            process = (
                f"{os.getuid()} /bin/bash /tmp/usb_watchdog.sh "
                f"--instance-token {self.TOKEN}\n"
            )
            with mock.patch.object(
                gui,
                "sh",
                side_effect=[
                    (0, process, ""),
                    (0, "Sun Aug 30 18:20:31 2026\n", ""),
                ],
            ):
                state = gui.read_instance(
                    str(state_path), now=1000 + gui.HEARTBEAT_STALE_SECONDS + 1
                )
        self.assertTrue(state["alive"])
        self.assertFalse(state["healthy"])
        self.assertFalse(state["operational"])

    def test_current_polling_state_remains_operational(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = self.write_state(directory, status="polling")
            process = (
                f"{os.getuid()} /bin/bash /tmp/usb_watchdog.sh "
                f"--instance-token {self.TOKEN} --dry-run\n"
            )
            with mock.patch.object(
                gui,
                "sh",
                side_effect=[
                    (0, process, ""),
                    (0, "Sun Aug 30 18:20:31 2026\n", ""),
                ],
            ):
                state = gui.read_instance(str(state_path), now=1005)
        self.assertTrue(state["alive"])
        self.assertFalse(state["healthy"])
        self.assertTrue(state["polling"])
        self.assertTrue(state["operational"])

    def test_probe_degraded_state_is_not_treated_as_polling_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = self.write_state(directory, status="degraded")
            process = (
                f"{os.getuid()} /bin/bash /tmp/usb_watchdog.sh "
                f"--instance-token {self.TOKEN} --dry-run\n"
            )
            with mock.patch.object(
                gui,
                "sh",
                side_effect=[
                    (0, process, ""),
                    (0, "Sun Aug 30 18:20:31 2026\n", ""),
                ],
            ):
                state = gui.read_instance(str(state_path), now=1005)
        self.assertFalse(state["polling"])
        self.assertFalse(state["operational"])

    def test_future_heartbeat_after_clock_rollback_is_not_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = self.write_state(directory, heartbeat=1001)
            process = (
                f"{os.getuid()} /bin/bash /tmp/usb_watchdog.sh "
                f"--instance-token {self.TOKEN}\n"
            )
            with mock.patch.object(
                gui,
                "sh",
                side_effect=[
                    (0, process, ""),
                    (0, "Sun Aug 30 18:20:31 2026\n", ""),
                ],
            ):
                state = gui.read_instance(str(state_path), now=1000)
        self.assertTrue(state["alive"])
        self.assertEqual(state["age"], -1)
        self.assertFalse(state["healthy"])

    def test_group_or_other_writable_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = self.write_state(directory)
            state_path.chmod(0o666)
            self.assertIsNone(gui.read_instance(str(state_path), now=1005))

    def test_symlinked_state_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            real = self.write_state(directory)
            link = Path(directory) / "linked.state"
            link.symlink_to(real)
            self.assertIsNone(gui.read_instance(str(link), now=1005))

    def test_only_known_state_paths_are_inspected(self):
        with mock.patch.object(gui, "ROOT_STATE", "/known/root"), mock.patch.object(
            gui, "DRY_STATE", "/known/dry"
        ), mock.patch.object(gui, "read_instance", return_value=None) as reader:
            self.assertEqual(gui.watchdog_instances(), [])
        self.assertEqual(
            [call.args[0] for call in reader.call_args_list],
            ["/known/root", "/known/dry"],
        )

    def test_legacy_state_defaults_to_graceful_shutdown_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = self.write_state(directory)
            lines = state_path.read_text(encoding="utf-8").splitlines()
            state_path.write_text(
                "\n".join(
                    line for line in lines if not line.startswith("shutdown_policy=")
                )
                + "\n",
                encoding="utf-8",
            )
            state = gui._read_state_data(str(state_path))
        self.assertEqual(state["shutdown_policy"], gui.GRACEFUL_THEN_FORCE)

    def test_invalid_state_shutdown_policy_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = self.write_state(directory, shutdown_policy="unknown")
            self.assertIsNone(gui._read_state_data(str(state_path)))

    def test_process_start_identity_is_read_in_utc_c_locale(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = self.write_state(directory)
            process = (
                f"{os.getuid()} /bin/bash /tmp/usb_watchdog.sh "
                f"--instance-token {self.TOKEN}\n"
            )
            with mock.patch.object(
                gui,
                "sh",
                side_effect=[
                    (0, process, ""),
                    (0, "Sun Aug 30 18:20:31 2026\n", ""),
                ],
            ) as runner:
                self.assertTrue(gui.read_instance(str(state_path), now=1005)["alive"])
        self.assertEqual(runner.call_args_list[1].args[0][:4], gui.PS_COMMAND)


class CommandTests(unittest.TestCase):
    def test_pretty_usb_device_hides_internal_descriptor_profile(self):
        line = (
            "USB:port=1048576 1234:5678 sn=SERIAL1 Composite Device | "
            "profile=usb=512;rev=257;device=0/0/0;packet=64;configs=1;"
            "interfaces=0:3/1/1"
        )
        self.assertEqual(
            gui.pretty_device(line),
            "USB · Composite Device (port 1048576) · #SERIAL1",
        )

    def test_arm_timeout_covers_worst_case_bounded_baseline(self):
        baseline_probes = 4
        startup_reconciliation_probes = 2
        snapshots_per_attempt = 2
        attempts = 4
        probe_timeout_seconds = 3
        worst_case_probe_seconds = (
            (baseline_probes + startup_reconciliation_probes)
            * snapshots_per_attempt
            * attempts
            * probe_timeout_seconds
        )
        self.assertGreater(gui.ARM_TIMEOUT_SECONDS, worst_case_probe_seconds)

    def test_shell_join_preserves_paths_with_spaces_and_quotes(self):
        command = ["/bin/bash", "/tmp/O'Brien Folder/script.sh", "plain"]
        joined = gui.shell_join(command)
        self.assertEqual(gui.shlex.split(joined), command)

    def test_subprocess_timeout_is_reported(self):
        started = time.monotonic()
        rc, _, error = gui.sh(["/bin/sleep", "2"], timeout=0.1)
        elapsed = time.monotonic() - started
        self.assertEqual(rc, 124)
        self.assertIn("timed out", error)
        self.assertLess(elapsed, 1.0)

    def test_private_log_is_created_with_owner_only_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "watchdog.log"
            with gui.open_private_log(log_path) as handle:
                handle.write(b"first\n")
            self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)
            self.assertEqual(log_path.read_text(encoding="utf-8"), "first\n")

    def test_private_log_repairs_existing_permissions_and_appends(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "watchdog.log"
            log_path.write_text("existing\n", encoding="utf-8")
            log_path.chmod(0o644)
            with gui.open_private_log(log_path) as handle:
                handle.write(b"new\n")
            self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)
            self.assertEqual(
                log_path.read_text(encoding="utf-8"), "existing\nnew\n"
            )

    def test_private_log_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target.log"
            target.write_text("do not change\n", encoding="utf-8")
            link = Path(directory) / "watchdog.log"
            link.symlink_to(target)
            with self.assertRaises(OSError):
                gui.open_private_log(link)
            self.assertEqual(target.read_text(encoding="utf-8"), "do not change\n")

    def test_privileged_launch_repairs_log_before_append(self):
        with tempfile.TemporaryDirectory() as directory:
            log_path = Path(directory) / "watchdog log.txt"
            log_path.write_text("existing\n", encoding="utf-8")
            log_path.chmod(0o644)
            launch = gui.privileged_log_launch(
                ["/bin/echo", "new entry"], str(log_path)
            )
            result = subprocess.run(
                ["/bin/sh", "-c", launch],
                text=True,
                capture_output=True,
                timeout=3,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            deadline = time.monotonic() + 1
            while "new entry" not in log_path.read_text(encoding="utf-8"):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.01)
            self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)

    def test_privileged_launch_rejects_symlink_log(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target.log"
            target.write_text("do not change\n", encoding="utf-8")
            link = Path(directory) / "watchdog.log"
            link.symlink_to(target)
            launch = gui.privileged_log_launch(["/bin/echo", "bad"], str(link))
            result = subprocess.run(
                ["/bin/sh", "-c", launch],
                text=True,
                capture_output=True,
                timeout=3,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unsafe watchdog log path", result.stderr)
            self.assertEqual(target.read_text(encoding="utf-8"), "do not change\n")

    def test_strict_wake_control_is_not_present(self):
        source = (ROOT / "usb_watchdog_gui.py").read_text(encoding="utf-8")
        self.assertNotIn("strict_wake", source)
        self.assertNotIn("--wake-policy", source)

    def test_launch_arguments_include_selected_shutdown_policy(self):
        app = gui.WatchdogApp.__new__(gui.WatchdogApp)
        app.dry_run = True
        app.shutdown_policy = gui.FORCE_IMMEDIATELY
        arguments = app._launch_arguments("abc123", "/tmp/watchdog.state")
        policy_index = arguments.index("--shutdown-policy")
        self.assertEqual(arguments[policy_index + 1], gui.FORCE_IMMEDIATELY)
        self.assertIn("--dry-run", arguments)

    def test_real_launch_drops_event_monitor_to_requesting_user(self):
        app = gui.WatchdogApp.__new__(gui.WatchdogApp)
        app.dry_run = False
        app.shutdown_policy = gui.GRACEFUL_THEN_FORCE
        arguments = app._launch_arguments("abc123", "/tmp/watchdog.state")
        uid_index = arguments.index("--event-monitor-uid")
        self.assertEqual(arguments[uid_index + 1], str(os.getuid()))

    def test_gui_defaults_to_dry_run_and_recommended_policy(self):
        self.assertTrue(gui.DEFAULT_DRY_RUN)
        app = gui.WatchdogApp.__new__(gui.WatchdogApp)
        app.dry_run = gui.DEFAULT_DRY_RUN
        app.shutdown_policy = gui.GRACEFUL_THEN_FORCE
        arguments = app._launch_arguments("abc123", "/tmp/watchdog.state")
        self.assertIn("--dry-run", arguments)
        policy_index = arguments.index("--shutdown-policy")
        self.assertEqual(arguments[policy_index + 1], gui.GRACEFUL_THEN_FORCE)

    def test_real_mode_confirmation_names_selected_response(self):
        app = gui.WatchdogApp.__new__(gui.WatchdogApp)
        app.shutdown_policy = gui.FORCE_IMMEDIATELY
        with mock.patch.object(gui.rumps, "alert", return_value=1) as alert:
            self.assertTrue(app._confirm_real_mode())
        self.assertIn("ungraceful forced halt", alert.call_args.args[1])

    def test_real_mode_confirmation_can_cancel(self):
        app = gui.WatchdogApp.__new__(gui.WatchdogApp)
        app.shutdown_policy = gui.GRACEFUL_THEN_FORCE
        with mock.patch.object(gui.rumps, "alert", return_value=0):
            self.assertFalse(app._confirm_real_mode())

    def test_settings_are_locked_while_an_instance_is_registered(self):
        app = gui.WatchdogApp.__new__(gui.WatchdogApp)
        app.dryrun_item = mock.Mock()
        app.graceful_shutdown_item = mock.Mock()
        app.immediate_halt_item = mock.Mock()
        app._set_settings_enabled(False)
        app.dryrun_item.set_callback.assert_called_once_with(None)
        app.graceful_shutdown_item.set_callback.assert_called_once_with(None)
        app.immediate_halt_item.set_callback.assert_called_once_with(None)

    def test_settings_are_restored_after_disarm(self):
        app = gui.WatchdogApp.__new__(gui.WatchdogApp)
        app.dryrun_item = mock.Mock()
        app.graceful_shutdown_item = mock.Mock()
        app.immediate_halt_item = mock.Mock()
        app._set_settings_enabled(True)
        app.dryrun_item.set_callback.assert_called_once_with(app.on_toggle_dryrun)
        app.graceful_shutdown_item.set_callback.assert_called_once_with(
            app.on_select_graceful_shutdown
        )
        app.immediate_halt_item.set_callback.assert_called_once_with(
            app.on_select_immediate_halt
        )

    def test_dry_run_launch_failure_is_reported(self):
        app = gui.WatchdogApp.__new__(gui.WatchdogApp)
        app.dry_run = True
        app.shutdown_policy = gui.GRACEFUL_THEN_FORCE
        app.refresh = mock.Mock()
        with mock.patch.object(gui.os.path, "isfile", return_value=True), mock.patch.object(
            gui.os, "makedirs"
        ), mock.patch.object(
            gui, "open_private_log", side_effect=OSError("log unavailable")
        ), mock.patch.object(gui.rumps, "alert") as alert:
            app.arm()
        self.assertEqual(alert.call_args.args[0], "Arm failed")
        self.assertIn("log unavailable", alert.call_args.args[1])
        app.refresh.assert_called_once_with(None)

    def test_disarm_failure_is_reported_without_claiming_success(self):
        instance = {
            "pid": 4321,
            "uid": os.getuid(),
            "path": "/tmp/watchdog.state",
            "token": "0123456789abcdef0123456789abcdef",
        }
        app = gui.WatchdogApp.__new__(gui.WatchdogApp)
        app._stop_instance = mock.Mock(return_value=(False, "stop rejected"))
        app.refresh = mock.Mock()
        with mock.patch.object(
            gui, "watchdog_instances", side_effect=[[instance], [], []]
        ), mock.patch.object(gui.rumps, "alert") as alert, mock.patch.object(
            gui, "notify"
        ) as notify:
            app.disarm()
        self.assertEqual(alert.call_args.args[0], "Disarm did not complete")
        self.assertIn("stop rejected", alert.call_args.args[1])
        notify.assert_not_called()
        app.refresh.assert_called_once_with(None)


if __name__ == "__main__":
    unittest.main()
