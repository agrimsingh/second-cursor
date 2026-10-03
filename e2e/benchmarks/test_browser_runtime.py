"""Offline coverage for visible fixture browsers; no browser, native app, or UI is launched."""
import hashlib
import pathlib
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import run as harness

launcher = harness.load_start_browser()


class BrowserLaunchTests(unittest.TestCase):
    def test_headed_launch_only_changes_visibility_flag(self):
        chrome, profile = pathlib.Path("/fixture/chrome"), pathlib.Path("/tmp/disposable-profile")
        headless = launcher.launch_command(chrome, profile)
        headed = launcher.launch_command(chrome, profile, headed=True)
        self.assertIn("--headless=new", headless)
        self.assertNotIn("--headless=new", headed)
        self.assertEqual([arg for arg in headless if arg != "--headless=new"], headed)
        self.assertIn(f"--user-data-dir={profile}", headed)
        self.assertIn(f"--load-extension={launcher.EXTENSION}", headed)
        self.assertEqual(headed[-1], "about:blank")

    def test_identity_pins_executable_and_version_without_opening_ui(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = (pathlib.Path(directory) / "chrome").resolve()
            executable.write_bytes(b"fixture browser binary")
            result = subprocess.CompletedProcess([], 0, "Chrome for Testing 149.0\n", "")
            with patch.object(launcher.subprocess, "run", return_value=result) as version:
                identity = launcher.browser_identity(executable)
            self.assertEqual(identity, {"binary": str(executable), "version": "Chrome for Testing 149.0",
                                        "sha256": hashlib.sha256(executable.read_bytes()).hexdigest()})
            version.assert_called_once_with([str(executable), "--version"], capture_output=True,
                                            text=True, check=True, timeout=5)

    def test_empty_browser_version_is_not_a_comparable_identity(self):
        with patch.object(launcher.subprocess, "run", return_value=types.SimpleNamespace(stdout="")):
            with self.assertRaisesRegex(RuntimeError, "did not report a version"):
                launcher.browser_identity("/fixture/chrome")

    def test_launch_and_stop_preserve_ownership_and_visibility(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            shared = root / "manifest.json"
            previous = b'{"name":"existing user registration"}'
            shared.write_bytes(previous)
            profile = root / "profile"
            profile.mkdir()
            identity = {"binary": "/fixture/chrome", "version": "Chrome for Testing 149.0", "sha256": "a" * 64}
            process = Mock(pid=912345)
            process.poll.return_value = 0
            with patch.object(launcher, "SHARED_MANIFEST", shared), \
                 patch.object(launcher, "find_chrome", return_value=pathlib.Path(identity["binary"])), \
                 patch.object(launcher, "browser_identity", return_value=identity), \
                 patch.object(launcher.os, "access", return_value=True), \
                 patch.object(launcher.tempfile, "mkdtemp", return_value=str(profile)), \
                 patch.object(launcher.subprocess, "Popen", return_value=process) as popen, \
                 patch.object(launcher.os, "killpg") as kill:
                browser = launcher.launch(sc_home="/tmp/disposable-sc", app="/fixture/app", headed=True)
                self.assertEqual(browser.metadata, {"identity": identity, "headed": True})
                self.assertNotIn("--headless=new", popen.call_args.args[0])
                self.assertEqual(popen.call_args.kwargs["env"]["SC_HOME"], "/tmp/disposable-sc")
                self.assertTrue(popen.call_args.kwargs["start_new_session"])
                browser.stop()
                self.assertEqual(shared.read_bytes(), previous)
                self.assertFalse(profile.exists())
                kill.assert_called_once_with(process.pid, launcher.signal.SIGKILL)

    def test_failed_spawn_restores_manifest_even_for_detached_cli_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            shared, profile = root / "manifest.json", root / "profile"
            previous = b'{"name":"existing user registration"}'
            shared.write_bytes(previous)
            profile.mkdir()
            with patch.object(launcher, "SHARED_MANIFEST", shared), \
                 patch.object(launcher, "find_chrome", return_value=pathlib.Path("/fixture/chrome")), \
                 patch.object(launcher, "browser_identity", return_value={}), \
                 patch.object(launcher.os, "access", return_value=True), \
                 patch.object(launcher.tempfile, "mkdtemp", return_value=str(profile)), \
                 patch.object(launcher.subprocess, "Popen", side_effect=OSError("spawn failed")):
                with self.assertRaisesRegex(OSError, "spawn failed"):
                    launcher.launch(sc_home="/tmp/disposable-sc", app="/fixture/app", restore_manifest=False,
                                    headed=True)
            self.assertEqual(shared.read_bytes(), previous)
            self.assertFalse(profile.exists())


class BrowserReadinessTests(unittest.TestCase):
    def instance(self, headed=False):
        instance = object.__new__(harness.TestInstance)
        instance.headed_browser, instance.browser = headed, None
        instance.home, instance.app = pathlib.Path("/tmp/sc-fixture"), pathlib.Path("/fixture/app")
        return instance

    def browser(self, headed=False):
        browser = Mock(pid=12345, headed=headed, version="Chrome for Testing 149.0")
        browser.running.return_value = True
        browser.metadata = {"identity": {"version": browser.version}, "headed": headed}
        return browser

    def test_headed_setting_reaches_launch_and_waits_for_extension(self):
        instance, browser = self.instance(headed=True), self.browser(headed=True)
        module = Mock()
        module.launch.return_value = browser
        with patch.object(harness, "load_start_browser", return_value=module), \
             patch.object(instance, "browser_connected", side_effect=[False, True]) as connected, \
             patch.object(harness.time, "sleep"):
            self.assertIs(instance.start_browser(), browser)
        module.launch.assert_called_once_with(sc_home=instance.home, app=instance.app, headed=True)
        self.assertEqual(connected.call_count, 2)

    def test_default_remains_headless(self):
        instance, browser = self.instance(), self.browser()
        module = Mock()
        module.launch.return_value = browser
        with patch.object(harness, "load_start_browser", return_value=module), \
             patch.object(instance, "browser_connected", return_value=True):
            instance.start_browser()
        self.assertFalse(module.launch.call_args.kwargs["headed"])

    def test_existing_browser_cannot_silently_claim_different_visibility(self):
        instance = self.instance()
        instance.browser = self.browser()
        with self.assertRaisesRegex(RuntimeError, "different visibility"):
            instance.start_browser(headed=True)

    def test_browser_exit_does_not_count_as_ready(self):
        instance = self.instance()
        instance.browser = self.browser()
        instance.browser.running.return_value = False
        with patch.object(harness, "tail", return_value="launch error"), \
             patch.object(instance, "browser_connected") as connected:
            with self.assertRaisesRegex(RuntimeError, "exited during startup"):
                instance.start_browser()
        connected.assert_not_called()

    def test_run_records_actual_runtime_identity_and_visibility(self):
        run = object.__new__(harness.Run)
        run.browser, run.notes = True, []
        run.instance = Mock()
        run.instance.browser = self.browser(headed=True)
        with patch("builtins.print"):
            run.start_browser_if_needed([])
        self.assertEqual(run.browser_runtime, {"pid": 12345, **run.instance.browser.metadata})
        self.assertIn("headed", run.notes[0])


if __name__ == "__main__":
    unittest.main()
