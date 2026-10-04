"""Extension evidence identifies loaded source bytes without launching a browser."""
import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import best_in_class as report
import model_benchmark as pilot


class ExtensionIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.extension = self.root / "repo" / "extension"
        (self.extension / "nested").mkdir(parents=True)
        self.manifest = {"manifest_version": 3, "name": "Fixture", "version": "0.3.0",
                         "background": {"service_worker": "background.js", "type": "module"}}
        (self.extension / "manifest.json").write_text(json.dumps(self.manifest))
        (self.extension / "background.js").write_text("export const fixture = true;")
        (self.extension / "nested" / "snapshot.js").write_text("export function snapshot() {}")
        (self.extension / "nested" / "module.mjs").write_text("export default 1;")
        (self.extension / "nested" / "icon.png").write_bytes(b"fixture pixels")

    def test_default_path_matches_launcher_and_ignores_unrelated_secret_files(self):
        (self.extension / "auth.json").write_text('{"token":"fixture-not-a-real-secret"}')
        original = pathlib.Path.read_bytes
        def checked(path):
            self.assertNotEqual(path.name, "auth.json")
            return original(path)
        with patch.dict(os.environ, {"SC_E2E_EXTENSION": ""}), \
                patch.object(report, "REPO", self.root / "repo"), \
                patch.object(pathlib.Path, "read_bytes", checked):
            identity = report.extension_identity()
        self.assertEqual(identity["path"], str(self.extension.resolve()))
        self.assertEqual(identity["version"], "0.3.0")
        self.assertEqual(identity["manifest_version"], 3)
        self.assertEqual(set(identity["files"]), {"manifest.json", "background.js", "nested/snapshot.js",
                                                 "nested/module.mjs", "nested/icon.png"})
        self.assertNotIn("token", json.dumps(identity))

    def test_override_matches_start_browser_and_identical_copies_keep_fingerprint(self):
        copied = self.root / "saved extension"
        shutil.copytree(self.extension, copied)
        identities, fingerprints = [], []
        for folder in (self.extension, copied):
            with patch.dict(os.environ, {"SC_E2E_EXTENSION": str(folder)}):
                identities.append(report.extension_identity())
                fingerprints.append(pilot.runner_fingerprint())
                spec = importlib.util.spec_from_file_location("extension_identity_launcher", pilot.ROOT / "start-browser.py")
                launcher = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(launcher)
                self.assertEqual(identities[-1]["path"], str(launcher.EXTENSION))
        self.assertNotEqual(identities[0]["path"], identities[1]["path"])
        self.assertEqual(identities[0]["sha256"], identities[1]["sha256"])
        self.assertEqual(fingerprints[0], fingerprints[1])

    def test_manifest_and_every_javascript_module_mutation_change_identity(self):
        with patch.dict(os.environ, {"SC_E2E_EXTENSION": str(self.extension)}):
            initial = report.extension_identity()["sha256"]
            initial_runner = pilot.runner_fingerprint()
            for relative in ("background.js", "nested/snapshot.js", "nested/module.mjs", "nested/icon.png", "manifest.json"):
                with self.subTest(relative=relative):
                    path = self.extension / relative
                    original = path.read_bytes()
                    if relative == "manifest.json":
                        # Same declared version, changed runtime permissions.
                        path.write_text(json.dumps({**self.manifest, "permissions": ["tabs"]}))
                    else:
                        path.write_bytes(original + b" changed")
                    self.assertNotEqual(report.extension_identity()["sha256"], initial)
                    self.assertNotEqual(pilot.runner_fingerprint(), initial_runner)
                    path.write_bytes(original)
            self.assertEqual(report.extension_identity()["sha256"], initial)

    def test_adding_or_renaming_script_changes_identity(self):
        with patch.dict(os.environ, {"SC_E2E_EXTENSION": str(self.extension)}):
            initial = report.extension_identity()["sha256"]
            script = self.extension / "nested" / "late.js"
            script.write_text("export default 2;")
            added = report.extension_identity()["sha256"]
            self.assertNotEqual(added, initial)
            script.rename(script.with_name("renamed.js"))
            self.assertNotEqual(report.extension_identity()["sha256"], added)

    def test_metadata_records_effective_path_and_hash_for_every_execution_profile(self):
        with patch.dict(os.environ, {"SC_E2E_EXTENSION": str(self.extension)}), \
                patch.object(report.subprocess, "run", return_value=types.SimpleNamespace(stdout="fixture")), \
                patch.object(report.platform, "platform", return_value="test-platform"), \
                patch.object(report, "digest", return_value="test-digest"):
            metadata = report.metadata()
        self.assertEqual(metadata["extension_identity"]["path"], str(self.extension.resolve()))
        self.assertRegex(metadata["extension_identity"]["sha256"], r"^[a-f0-9]{64}$")

    def test_missing_or_invalid_override_does_not_fall_back_to_installed_extension(self):
        with patch.dict(os.environ, {"SC_E2E_EXTENSION": str(self.root / "missing")}), self.assertRaises(FileNotFoundError):
            report.extension_identity()
        (self.extension / "manifest.json").write_text('{"manifest_version":3}')
        with patch.dict(os.environ, {"SC_E2E_EXTENSION": str(self.extension)}), self.assertRaises(ValueError):
            report.extension_identity()


if __name__ == "__main__":
    unittest.main()
