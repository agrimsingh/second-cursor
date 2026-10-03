#!/usr/bin/env python3
"""Process-level stdio transport checks against a disposable HTTP server; no desktop access.

Run `python3 e2e/transport_stdio.py` for the Node proxy. Add
`--native-binary /path/to/SecondCursor` to exercise the compiled native proxy too.
The fake server enforces session headers so these tests cannot pass through legacy fallback.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
COMMANDS = []


def rpc(identifier, method, params=None):
    message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if identifier is not None:
        message["id"] = identifier
    return message


class FakeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, mode="normal", port=0):
        super().__init__(("127.0.0.1", port), Handler)
        self.mode = mode
        self.events = []
        self.sessions = set()
        self.counter = 0
        self.active = 0
        self.lock = threading.Lock()

    def record(self, kind, **values):
        with self.lock:
            self.events.append({"kind": kind, **values})


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def reply(self, status, payload=None, session=None):
        body = json.dumps(payload).encode() if payload is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        if session:
            self.send_header("Mcp-Session-Id", session)
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_POST(self):
        message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        identifier = message.get("id")
        method = message["method"]
        session = self.headers.get("Mcp-Session-Id")
        self.server.record("post", method=method, id=identifier, session=session,
                           protocol=self.headers.get("MCP-Protocol-Version"), message=message)
        if method == "initialize":
            time.sleep(0.1)  # Guarantees pipelined messages arrive before the handshake completes.
            if self.server.mode == "unauthorized":
                self.reply(401, {"jsonrpc": "2.0", "id": identifier,
                                 "error": {"code": -32000, "message": "Unauthorized token"}})
                return
            if session and session not in self.server.sessions:
                self.reply(404, {"jsonrpc": "2.0", "id": identifier,
                                 "error": {"code": -32001, "message": "Unknown MCP session; initialize again"}})
                return
            if not session:
                with self.server.lock:
                    self.server.counter += 1
                    session = f"runtime-{self.server.counter}"
                    self.server.sessions.add(session)
            self.reply(200, {"jsonrpc": "2.0", "id": identifier,
                             "result": {"protocolVersion": "2025-11-25", "capabilities": {},
                                        "serverInfo": {"name": "fake-second-cursor", "version": "1"}}},
                       session=None if self.server.mode == "missing_header" else session)
            return
        if session not in self.server.sessions:
            self.reply(404, {"jsonrpc": "2.0", "id": identifier,
                             "error": {"code": -32001, "message": "Unknown MCP session; initialize again"}})
            return
        name = message.get("params", {}).get("name")
        if name == "drop_response":
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        if name == "stale":
            with self.server.lock:
                self.server.sessions.remove(session)
            self.reply(404, {"jsonrpc": "2.0", "id": identifier,
                             "error": {"code": -32001, "message": "Unknown MCP session; initialize again"}})
            return
        with self.server.lock:
            self.server.active += 1
        time.sleep(0.3 if name == "slow" else 0.01)
        with self.server.lock:
            self.server.active -= 1
        self.server.record("completed", id=identifier, session=session)
        self.reply(202 if identifier is None else 200, None if identifier is None else
                   {"jsonrpc": "2.0", "id": identifier, "result": {"session": session}})

    def do_DELETE(self):
        session = self.headers.get("Mcp-Session-Id")
        with self.server.lock:
            active = self.server.active
            known = session in self.server.sessions
            self.server.sessions.discard(session)
        self.server.record("delete", session=session, active=active, path=self.path)
        self.reply(204 if known else 404)


@contextmanager
def fixture(mode="normal"):
    server = FakeServer(mode)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory(prefix="sc-stdio-") as home:
        Path(home, "config.json").write_text(json.dumps({
            "port": server.server_port, "token": "disposable-token",
            "blockedBundleIDs": [], "confirmationRequiredTools": [],
        }))
        try:
            yield server, {**os.environ, "SC_HOME": home}
        finally:
            server.shutdown()
            server.server_close()


def run(command, messages, environment):
    data = "".join((item if isinstance(item, str) else json.dumps(item)) + "\n" for item in messages)
    result = subprocess.run(command, input=data, env=environment, text=True,
                            capture_output=True, timeout=15)
    if result.returncode != 0:
        raise AssertionError(f"Proxy exited {result.returncode}: {result.stderr}")
    return [json.loads(line) for line in result.stdout.splitlines()]


def start(command, environment):
    return subprocess.Popen(command, env=environment, text=True, stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def exchange(process, message):
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()
    # A timer makes failures bounded even when a proxy regresses into a deadlock.
    timer = threading.Timer(10, process.kill)
    timer.start()
    try:
        line = process.stdout.readline()
        if not line:
            raise AssertionError(f"Proxy exited or failed to respond: {process.stderr.read()}")
        return json.loads(line)
    finally:
        timer.cancel()


def finish(process):
    process.stdin.close()
    process.stdin = None
    out, err = process.communicate(timeout=15)
    if process.returncode != 0:
        raise AssertionError(f"Proxy exited {process.returncode}: {err}")
    return out


class StdioTransportTests(unittest.TestCase):
    def test_pipelined_handshake_concurrency_cancellation_and_eof(self):
        for name, command in COMMANDS:
            with self.subTest(proxy=name), fixture() as (server, environment):
                output = run(command, [rpc(1, "initialize"), rpc(None, "notifications/initialized"),
                    rpc(2, "tools/call", {"name": "slow"}), rpc(3, "tools/call", {"name": "fast"}),
                    rpc(None, "notifications/cancelled", {"requestId": 2})], environment)
                self.assertEqual([item["id"] for item in output], [1, 3, 2])
                self.assertTrue(all("result" in item for item in output))
                posts = [event for event in server.events if event["kind"] == "post"]
                self.assertEqual(len(posts), 5)
                self.assertIsNone(posts[0]["session"])
                self.assertTrue(all(event["session"] == "runtime-1" for event in posts[1:]))
                self.assertTrue(all(event["protocol"] == "2025-11-25" for event in posts[1:]))
                self.assertEqual(server.events[-1], {"kind": "delete", "session": "runtime-1", "active": 0, "path": "/mcp"})
                self.assertEqual(server.sessions, set())

    def test_initialization_failure_does_not_dispatch_or_cleanup(self):
        for name, command in COMMANDS:
            for mode in ("unauthorized", "missing_header"):
                with self.subTest(proxy=name, mode=mode), fixture(mode) as (server, environment):
                    output = run(command, [rpc(1, "initialize"), rpc(2, "tools/call", {"name": "mutate"})], environment)
                    self.assertEqual([item["id"] for item in output], [1, 2])
                    self.assertTrue(all("error" in item for item in output))
                    self.assertEqual(len(server.events), 1)
                    self.assertEqual(server.events[0]["method"], "initialize")

    def test_missing_initialize_and_invalid_input_remain_local(self):
        for name, command in COMMANDS:
            with self.subTest(proxy=name), fixture() as (server, environment):
                output = run(command, [rpc(1, "tools/call", {"name": "mutate"}), "{bad", "[]"], environment)
                self.assertEqual([item["error"]["code"] for item in output], [-32000, -32700, -32600])
                self.assertEqual(server.events, [])

    def test_duplicate_initialize_does_not_leak_session(self):
        for name, command in COMMANDS:
            with self.subTest(proxy=name), fixture() as (server, environment):
                output = run(command, [rpc(1, "initialize"), rpc(2, "initialize"), rpc(3, "tools/list")], environment)
                by_id = {item["id"]: item for item in output}
                self.assertIn("result", by_id[1])
                self.assertIn("error", by_id[2])
                self.assertIn("result", by_id[3])
                self.assertEqual(server.counter, 1)
                self.assertEqual(server.sessions, set())

    def test_stale_session_requires_explicit_reinitialize_without_retry(self):
        for name, command in COMMANDS:
            with self.subTest(proxy=name), fixture() as (server, environment):
                process = start(command, environment)
                try:
                    self.assertIn("result", exchange(process, rpc(1, "initialize")))
                    self.assertIn("error", exchange(process, rpc(2, "tools/call", {"name": "stale"})))
                    self.assertIn("error", exchange(process, rpc(3, "tools/call", {"name": "must-not-run"})))
                    self.assertEqual(len([event for event in server.events if event["kind"] == "post"]), 2)
                    self.assertIn("result", exchange(process, rpc(4, "initialize")))
                    self.assertEqual(exchange(process, rpc(5, "tools/list"))["result"]["session"], "runtime-2")
                    self.assertEqual(finish(process), "")
                    self.assertEqual([event["session"] for event in server.events if event["kind"] == "delete"], ["runtime-2"])
                    self.assertEqual(server.counter, 2)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()

    def test_eof_closes_only_its_own_session(self):
        for name, command in COMMANDS:
            with self.subTest(proxy=name), fixture() as (server, environment):
                first, second = start(command, environment), start(command, environment)
                try:
                    self.assertIn("result", exchange(first, rpc(1, "initialize")))
                    self.assertIn("result", exchange(second, rpc(1, "initialize")))
                    self.assertEqual(finish(first), "")
                    self.assertEqual(server.sessions, {"runtime-2"})
                    self.assertEqual(exchange(second, rpc(2, "tools/list"))["result"]["session"], "runtime-2")
                    self.assertEqual(finish(second), "")
                    self.assertEqual(server.sessions, set())
                    self.assertEqual([event["session"] for event in server.events if event["kind"] == "delete"], ["runtime-1", "runtime-2"])
                finally:
                    for process in (first, second):
                        if process.poll() is None:
                            process.kill()
                            process.communicate()

    def test_dropped_mutation_response_is_not_retried(self):
        for name, command in COMMANDS:
            with self.subTest(proxy=name), fixture() as (server, environment):
                output = run(command, [rpc(1, "initialize"),
                    rpc(2, "tools/call", {"name": "drop_response"})], environment)
                self.assertEqual([item["id"] for item in output], [1, 2])
                self.assertIn("error", output[1])
                self.assertEqual(len([event for event in server.events if event["kind"] == "post"]), 2)
                self.assertEqual(server.events[-1]["kind"], "delete")

    def test_connection_refused_allows_explicit_reinitialize(self):
        for name, command in COMMANDS:
            with self.subTest(proxy=name), fixture() as (server, environment):
                process = start(command, environment)
                recovered = None
                try:
                    self.assertIn("result", exchange(process, rpc(1, "initialize")))
                    server.shutdown()
                    server.server_close()
                    self.assertIn("error", exchange(process, rpc(2, "tools/call", {"name": "mutate"})))
                    self.assertFalse(Path(environment["SC_HOME"], "instance.pid").exists())
                    recovered = FakeServer(port=server.server_port)
                    recovered.counter = 1
                    recovered.sessions.add("runtime-1")  # The real app restores transport sessions from disk.
                    threading.Thread(target=recovered.serve_forever, daemon=True).start()
                    self.assertIn("result", exchange(process, rpc(3, "initialize")))
                    self.assertEqual(exchange(process, rpc(4, "tools/list"))["result"]["session"], "runtime-1")
                    self.assertEqual(recovered.events[0]["session"], "runtime-1")
                    self.assertEqual(recovered.counter, 1)
                    self.assertEqual(finish(process), "")
                    self.assertEqual(recovered.sessions, set())
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()
                    if recovered:
                        recovered.shutdown()
                        recovered.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-binary", type=Path)
    args, remaining = parser.parse_known_args()
    node = shutil.which("node")
    if not node:
        parser.error("Node is required to check sdk/mcp-stdio.mjs")
    COMMANDS.append(("node", [node, str(ROOT / "sdk/mcp-stdio.mjs")]))
    if args.native_binary:
        COMMANDS.append(("native", [str(args.native_binary.resolve()), "mcp"]))
    unittest.main(argv=[__file__, *remaining], verbosity=2)
