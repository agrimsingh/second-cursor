"""Production ExtensionLink socket tests. No application, browser, or GUI starts."""
import json
import os
from pathlib import Path
import select
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest

REPO = Path(__file__).resolve().parents[1]
PROBE = r'''
import Foundation
import SCShared

final class DispatchCount: @unchecked Sendable {
    private let lock = NSLock()
    private var count = 0
    func note() { lock.lock(); count += 1; lock.unlock() }
    var value: Int { lock.lock(); defer { lock.unlock() }; return count }
}
@main struct Probe {
    static func emit(_ value: [String: Any]) throws {
        var data = try JSONSerialization.data(withJSONObject: value)
        data.append(10)
        FileHandle.standardOutput.write(data)
    }
    static func main() async throws {
        let link = ExtensionLink(socketURL: URL(fileURLWithPath: CommandLine.arguments[1]))
        try link.start()
        try emit(["ready": true])
        while let line = readLine() {
            let input = try JSONSerialization.jsonObject(with: Data(line.utf8)) as! [String: Any]
            if input["method"] as? String == "instances" {
                let instances = try JSONSerialization.jsonObject(with: link.instances())
                try emit(["result": instances])
                continue
            }
            let dispatch = DispatchCount()
            let context = ExecutionContext(browserID: input["browser_id"] as? String) { dispatch.note() }
            do {
                let result = try await Execution.$context.withValue(context) {
                    try await link.call(input["method"] as! String, input["params"] as? [String: Any] ?? [:], timeout: 2)
                }
                try emit(["result": result, "dispatches": dispatch.value])
            } catch {
                try emit(["error": error.localizedDescription, "dispatches": dispatch.value])
            }
        }
    }
}
'''


class Peer:
    def __init__(self, path, instance, capabilities):
        self.messages = []
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.connect(str(path))
        self.socket.sendall((json.dumps({"type": "hello", "instance_id": instance,
                                        "capabilities": capabilities, "version": "test"}) + "\n").encode())
        self.reader = self.socket.makefile("rb")
        self.thread = threading.Thread(target=self._respond, daemon=True)
        self.thread.start()

    def _respond(self):
        try:
            for line in self.reader:
                message = json.loads(line)
                if "method" not in message:
                    continue
                self.messages.append(message)
                self.socket.sendall((json.dumps({"id": message["id"], "result": {"ok": True}}) + "\n").encode())
        except (OSError, ValueError):
            pass

    def close(self):
        try:
            self.socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.socket.close()
        self.thread.join(timeout=2)
        self.reader.close()


class BrowserExpectationCapabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        products = next((REPO / ".build" / mode for mode in ("debug", "release")
                         if (REPO / ".build" / mode / "SCShared.o").is_file()), None)
        if not products or not shutil.which("xcrun"):
            raise unittest.SkipTest("Build Swift dependencies once to test the production browser link")
        cls.build = tempfile.TemporaryDirectory(prefix="sc-expect-link-build-")
        scratch = Path(cls.build.name)
        main = scratch / "probe.swift"
        main.write_text(PROBE)
        cls.binary = scratch / "probe"
        compiled = subprocess.run([
            "xcrun", "swiftc", "-parse-as-library", "-I", str(products),
            "-module-cache-path", str(scratch / "module-cache"),
            str(REPO / "Sources/SCBrowser/ExtensionLink.swift"), str(main),
            str(products / "SCShared.o"), "-o", str(cls.binary),
        ], text=True, capture_output=True, timeout=120)
        if compiled.returncode:
            cls.build.cleanup()
            raise AssertionError(compiled.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.build.cleanup()

    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory(prefix="sc-expect-link-")
        # macOS sockaddr_un limits the path to 104 bytes; use /tmp rather than the long per-user TMPDIR.
        self.socket_dir = tempfile.TemporaryDirectory(prefix="sc-expect-", dir="/tmp")
        self.path = Path(self.socket_dir.name) / "browser.sock"
        self.peers = []
        self.proc = subprocess.Popen([str(self.binary), str(self.path)], text=True,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     env={**os.environ, "SC_HOME": self.scratch.name})
        self.assertEqual(self._read(), {"ready": True})

    def tearDown(self):
        for peer in self.peers:
            peer.close()
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(timeout=5)
        self.proc.stdout.close()
        self.proc.stderr.close()
        self.scratch.cleanup()
        self.socket_dir.cleanup()

    def _read(self):
        self.assertTrue(select.select([self.proc.stdout], [], [], 5)[0], "Probe did not return")
        line = self.proc.stdout.readline()
        self.assertTrue(line, "Probe exited before responding")
        return json.loads(line)

    def request(self, method, params=None, browser=None):
        self.proc.stdin.write(json.dumps({"method": method, "params": params or {}, "browser_id": browser}) + "\n")
        self.proc.stdin.flush()
        return self._read()

    def peer(self, instance="old", capabilities=()):
        peer = Peer(self.path, instance, list(capabilities))
        self.peers.append(peer)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            listed = self.request("instances")["result"]["browsers"]
            if any(row["id"] == instance and row["capabilities"] == list(capabilities) for row in listed):
                return peer
            time.sleep(0.01)
        self.fail("Extension hello was not registered")

    def test_older_extension_still_receives_ordinary_input(self):
        peer = self.peer()
        reply = self.request("tab.click", {"tabId": 1, "ref": "e1"})
        self.assertEqual(reply["result"], {"ok": True})
        self.assertEqual(reply["dispatches"], 1)
        self.assertEqual(len(peer.messages), 1)

    def test_old_extension_refuses_expect_before_dispatch_or_input(self):
        peer = self.peer()
        for method in ("tab.click", "tab.type", "tab.pressKey", "tab.scroll", "tab.expectPrepare", "tab.expectFinish"):
            with self.subTest(method=method):
                params = {"tabId": 1, "expect": {"text": "Saved"}} if "expect" not in method else {"tabId": 1}
                reply = self.request(method, params)
                self.assertIn("reload", reply["error"])
                self.assertEqual(reply["dispatches"], 0)
        self.assertEqual(peer.messages, [])

    def test_new_extension_receives_expect(self):
        peer = self.peer("new", ["browser_expect_v1"])
        reply = self.request("tab.click", {"tabId": 1, "ref": "e1", "expect": {"text": "Saved"}})
        self.assertEqual(reply["dispatches"], 1)
        self.assertEqual(peer.messages[0]["params"]["expect"], {"text": "Saved"})

    def test_feature_on_another_browser_cannot_authorize_the_selected_old_one(self):
        old = self.peer("old")
        new = self.peer("new", ["browser_expect_v1"])
        reply = self.request("tab.click", {"tabId": 1, "expect": {"text": "Saved"}}, browser="old")
        self.assertIn("reload", reply["error"])
        self.assertEqual(reply["dispatches"], 0)
        self.assertEqual(old.messages, [])
        self.assertEqual(new.messages, [])
        passed = self.request("tab.click", {"tabId": 1, "expect": {"text": "Saved"}}, browser="new")
        self.assertEqual(passed["dispatches"], 1)
        self.assertEqual(len(new.messages), 1)

    def test_ambiguous_browser_still_refuses_without_dispatch(self):
        self.peer("old")
        self.peer("new", ["browser_expect_v1"])
        reply = self.request("tab.click", {"tabId": 1, "expect": {"text": "Saved"}})
        self.assertIn("Multiple browsers", reply["error"])
        self.assertEqual(reply["dispatches"], 0)
        self.assertTrue(all(not peer.messages for peer in self.peers))

    def test_reconnected_older_extension_cannot_inherit_the_capability(self):
        original = self.peer("same", ["browser_expect_v1"])
        replacement = self.peer("same", [])
        reply = self.request("tab.click", {"tabId": 1, "expect": {"text": "Saved"}})
        self.assertIn("reload", reply["error"])
        self.assertEqual(reply["dispatches"], 0)
        self.assertEqual(original.messages, [])
        self.assertEqual(replacement.messages, [])


if __name__ == "__main__":
    unittest.main()
