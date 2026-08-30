import importlib.util
import os
from pathlib import Path
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
    def test_arm_timeout_covers_worst_case_bounded_baseline(self):
        worst_case_probe_seconds = 4 * 2 * 4 * 3
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

    def test_strict_wake_control_is_not_present(self):
        source = (ROOT / "usb_watchdog_gui.py").read_text(encoding="utf-8")
        self.assertNotIn("strict_wake", source)
        self.assertNotIn("--wake-policy", source)


if __name__ == "__main__":
    unittest.main()
