"""Compile and exercise the production visual-readiness policy without opening apps."""

from pathlib import Path
import subprocess
import tempfile
import unittest


class NativeVisualPolicyTests(unittest.TestCase):
    def test_delayed_frames_noise_tiny_changes_and_deadlines(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="sc-visual-policy-") as temporary:
            binary = Path(temporary) / "visual-policy"
            subprocess.run([
                "xcrun", "swiftc", "-o", str(binary),
                str(root / "Sources/SCCore/VisualSettling.swift"),
                str(root / "e2e/native_visual_policy.swift"),
            ], check=True, capture_output=True, text=True)
            result = subprocess.run([str(binary)], check=True, capture_output=True, text=True)
            self.assertIn("PASS:", result.stdout)


if __name__ == "__main__":
    unittest.main()
