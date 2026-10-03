"""Model-host containment and protocol tests. These never start an app or model."""
import base64
import dataclasses
import json
import pathlib
import signal
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from benchmarks import model_host as host


VERSION = "codex-cli 0.159.0-alpha.12.1"
TOOL = {"name": "fixture_probe", "description": "Synthetic fixture only.",
        "inputSchema": {"type": "object", "properties": {}}}


def tool_call(name="fixture_probe", call_id="call-1", **changes):
    params = {"threadId": "thread-1", "turnId": "turn-1", "callId": call_id,
              "tool": name, "arguments": {"value": 1}}
    params.update(changes)
    return {"id": "tool-rpc-" + call_id, "method": "item/tool/call", "params": params}


def completed(status="completed", **changes):
    turn = {"id": "turn-1", "status": status, "items": [
        {"id": "answer-1", "type": "agentMessage", "phase": "final_answer", "text": "Finished."}]}
    turn.update(changes)
    return {"method": "turn/completed", "params": {"threadId": "thread-1", "turn": turn}}


class FakeRPC:
    def __init__(self, events, config=None, thread=None, inventory=None,
                 early_thread_events=(), early_turn_events=()):
        self.events = list(events)
        self.requests = []
        self.sent = []
        self.closed = False
        self.config = config or {"features": {name: False for name in host.DISABLED_FEATURES},
                                 "web_search": "disabled", "mcp_servers": {"personal": {"enabled": True}}}
        self.thread = thread
        self.early_thread_events = early_thread_events
        self.early_turn_events = early_turn_events
        self.inventory = inventory or {"data": [{"name": "personal", "tools": {},
                                                 "resources": [], "resourceTemplates": []}], "nextCursor": None}

    def request(self, method, params, deadline, notify):
        self.requests.append((method, params))
        if method == "initialize":
            return {"codexHome": "/synthetic-auth-home"}
        if method == "config/read":
            return {"config": self.config}
        if method == "thread/start":
            for event in self.early_thread_events:
                notify(event)
            if self.thread:
                return self.thread
            return {"thread": {"id": "thread-1", "ephemeral": True, "environments": []},
                    "model": "fixture-model", "reasoningEffort": "high", "runtimeWorkspaceRoots": [],
                    "approvalPolicy": "never", "instructionSources": []}
        if method == "mcpServerStatus/list":
            return self.inventory
        if method == "turn/start":
            for event in self.early_turn_events:
                notify(event)
            return {"turn": {"id": "turn-1"}}
        raise AssertionError(method)

    def receive(self, deadline):
        if not self.events:
            raise host.HostError("Synthetic disconnected stream")
        return self.events.pop(0)

    def send(self, message):
        self.sent.append(message)

    def close(self):
        self.closed = True


class HostProtocolTests(unittest.TestCase):
    def run_host(self, events, execute=None, config=None, **fake_options):
        config = config or host.HostConfig("fixture-model", "high", 3, 2, VERSION)
        rpc = FakeRPC(events, **fake_options)
        received = []
        used_dirs = []

        def factory(args, cwd):
            used_dirs.append(cwd)
            self.assertTrue(pathlib.Path(cwd).is_dir())
            self.assertNotEqual(pathlib.Path(cwd), pathlib.Path.cwd())
            return rpc

        with mock.patch.object(host, "host_version", return_value=VERSION), \
             mock.patch.object(host, "_RPC", side_effect=factory), \
             mock.patch.object(host.CodexModelHost, "_catalog", return_value=(pathlib.Path("/fake/catalog"), "digest")):
            result = host.CodexModelHost(config).run("Complete the synthetic fixture.", [TOOL],
                        execute or (lambda name, args: {"content": [{"type": "text", "text": "ok"}]}), received.append)
        self.assertTrue(rpc.closed)
        self.assertTrue(all(not pathlib.Path(path).exists() for path in used_dirs))
        return result, rpc, received

    def test_complete_round_trip_preserves_images_usage_and_unknown_provider_time(self):
        png = base64.b64encode(b"synthetic png transport bytes").decode()
        result, rpc, events = self.run_host([
            tool_call(), {"method": "thread/tokenUsage/updated", "params": {
                "threadId": "thread-1", "turnId": "turn-1", "tokenUsage": {"total": {"inputTokens": 9, "outputTokens": 3}}}},
            completed()], lambda name, args: {"content": [{"type": "text", "text": "state"},
                                                         {"type": "image", "mimeType": "image/png", "data": png}]})
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.final_response, "Finished.")
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(result.usage["inputTokens"], 9)
        self.assertIsNone(result.model_ms)
        self.assertTrue(result.isolation["verified"])
        response = next(message["result"] for message in rpc.sent if "result" in message)
        self.assertEqual(response["contentItems"][1], {"type": "inputImage", "imageUrl": "data:image/png;base64," + png})
        request = dict(rpc.requests)["thread/start"]
        self.assertEqual(request["config"]["mcp_servers"], {"personal": {"enabled": False}})
        self.assertEqual(request["environments"], [])
        self.assertTrue(request["ephemeral"])
        self.assertFalse(request["allowProviderModelFallback"])
        self.assertEqual([event["type"] for event in events].count("tool"), 1)

    def test_wrong_model_or_reasoning_never_reaches_actuator(self):
        for change in ({"model": "fallback"}, {"reasoningEffort": "low"}, {"runtimeWorkspaceRoots": ["/repo"]}):
            with self.subTest(change=change):
                thread = FakeRPC([]).request("thread/start", {}, 0, None)
                thread.update(change)
                callback = mock.Mock()
                result, _, _ = self.run_host([tool_call(), completed()], callback, thread=thread)
                self.assertEqual(result.status, "isolation_error")
                callback.assert_not_called()

    def test_mcp_inventory_leak_never_starts_turn(self):
        result, rpc, _ = self.run_host([completed()], inventory={"data": [{"tools": {"shell": {}}}]})
        self.assertEqual(result.status, "isolation_error")
        self.assertNotIn("turn/start", dict(rpc.requests))

    def test_inherited_repository_instructions_rejected(self):
        thread = FakeRPC([]).request("thread/start", {}, 0, None)
        thread["instructionSources"] = ["/some-repository/AGENTS.md"]
        result, rpc, _ = self.run_host([completed()], thread=thread)
        self.assertEqual(result.status, "isolation_error")
        self.assertNotIn("turn/start", dict(rpc.requests))

    def test_unknown_tool_duplicate_and_cross_thread_calls_never_execute(self):
        cases = [[tool_call(name="shell")], [tool_call(threadId="other")],
                 [tool_call(namespace="untrusted")], [tool_call(), tool_call()]]
        for events in cases:
            with self.subTest(events=events):
                callback = mock.Mock(return_value="ok")
                result, _, _ = self.run_host(events + [completed()], callback)
                self.assertEqual(result.status, "isolation_error")
                self.assertLessEqual(callback.call_count, 1)
                self.assertFalse(result.isolation.get("verified"))

    def test_human_input_request_is_denied_without_answer_or_actuator(self):
        callback = mock.Mock()
        event = {"id": "question", "method": "item/tool/requestUserInput",
                 "params": {"threadId": "thread-1", "turnId": "turn-1", "questions": []}}
        result, rpc, _ = self.run_host([event, completed()], callback)
        self.assertEqual(result.status, "isolation_error")
        callback.assert_not_called()
        self.assertFalse(any(message.get("id") == "question" for message in rpc.sent))

    def test_step_limit_prevents_the_next_action(self):
        config = host.HostConfig("fixture-model", "high", 1, 2, VERSION)
        callback = mock.Mock(return_value="ok")
        result, _, _ = self.run_host([tool_call(), tool_call(call_id="call-2"), completed()], callback, config)
        self.assertEqual(result.status, "step_limit")
        self.assertEqual(callback.call_count, 1)
        self.assertEqual(result.tool_calls, 1)

    def test_callback_deadline_interrupts_and_restores_alarm(self):
        previous = signal.getsignal(signal.SIGALRM)
        config = host.HostConfig("fixture-model", "high", 2, .03, VERSION)
        result, _, events = self.run_host([tool_call(), completed()], lambda name, args: time.sleep(2), config)
        self.assertEqual(result.status, "timeout")
        self.assertLess(result.wall_ms, 1000)
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)
        self.assertEqual(next(event for event in events if event["type"] == "tool")["status"], "timeout")

    def test_stream_failure_and_interruption_do_not_complete(self):
        for events in ([], [completed("interrupted")], [completed("failed")]):
            with self.subTest(events=events):
                result, _, _ = self.run_host(events)
                self.assertEqual(result.status, "failed")

    def test_model_reroute_invalidates_trial(self):
        result, _, _ = self.run_host([{"method": "model/rerouted", "params": {
            "threadId": "thread-1", "turnId": "turn-1", "toModel": "fallback"}}, completed()])
        self.assertEqual(result.status, "isolation_error")
        self.assertEqual(result.model, "fallback")

    def test_unexpected_builtin_action_invalidates_trial(self):
        result, _, _ = self.run_host([{"method": "item/started", "params": {
            "threadId": "thread-1", "turnId": "turn-1", "item": {"type": "commandExecution"}}}, completed()])
        self.assertEqual(result.status, "isolation_error")

    def test_duplicate_final_notification_is_emitted_once(self):
        item = completed()["params"]["turn"]["items"][0]
        result, _, events = self.run_host([{"method": "item/completed", "params": {
            "threadId": "thread-1", "turnId": "turn-1", "item": item}}, completed()])
        self.assertEqual(result.final_response, "Finished.")
        self.assertEqual([event["type"] for event in events].count("assistant"), 1)

    def test_notifications_before_start_responses_validate_after_binding(self):
        result, _, _ = self.run_host([tool_call(), completed()], early_thread_events=[
            {"method": "thread/status/changed", "params": {"threadId": "thread-1", "status": "idle"}},
            {"method": "thread/started", "params": {"thread": {"id": "thread-1"}}}],
            early_turn_events=[{"method": "turn/started", "params": {
                "threadId": "thread-1", "turn": {"id": "turn-1"}}}])
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.tool_calls, 1)

    def test_early_turn_completion_is_not_lost(self):
        result, _, _ = self.run_host([], early_turn_events=[completed()])
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.final_response, "Finished.")

    def test_mismatched_buffered_notifications_and_early_requests_still_reject(self):
        cases = [
            {"early_thread_events": [{"method": "thread/status/changed", "params": {"threadId": "other"}}]},
            {"early_thread_events": [{"method": "thread/started", "params": {"thread": {"id": "other"}}}]},
            {"early_turn_events": [{"method": "turn/started", "params": {
                "threadId": "thread-1", "turn": {"id": "other"}}}]},
            {"early_turn_events": [tool_call()]},
        ]
        for early in cases:
            with self.subTest(early=early):
                callback = mock.Mock()
                result, _, _ = self.run_host([completed()], callback, **early)
                self.assertEqual(result.status, "isolation_error")
                callback.assert_not_called()


class HostInputTests(unittest.TestCase):
    def test_configuration_and_tool_names_are_explicit(self):
        for args in (("", "high", 2, 5), ("m", "unknown", 2, 5), ("m", "high", 0, 5),
                     ("m", "high", 2, float("nan")), ("m", "high", True, 5)):
            with self.assertRaises(ValueError):
                host.HostConfig(*args)
        for tools in ([], [TOOL, TOOL], [{**TOOL, "name": "a.b"}]):
            with self.assertRaises(ValueError):
                host._tool_specs(tools)

    def test_missing_version_fails_before_process_start(self):
        with mock.patch.object(host, "_RPC") as rpc:
            with self.assertRaises(host.HostError):
                host.CodexModelHost(host.HostConfig("m", "high", 1, 3)).run("task", [TOOL], mock.Mock())
            rpc.assert_not_called()

    def test_unknown_image_payload_is_not_silently_dropped(self):
        for image in ({"type": "image", "mimeType": "image/png", "data": "not-base64"},
                      {"type": "image", "mimeType": "image/svg+xml", "data": "YWJj"},
                      {"type": "resource_link", "uri": "file:///judge"}):
            with self.assertRaises(host.HostError):
                host.tool_response({"content": [image]})

    def test_error_content_stays_error_and_structured_data_survives(self):
        result = host.tool_response({"content": [{"type": "text", "text": "not ready"}],
                                     "structuredContent": {"ready": False}, "isError": True})
        self.assertFalse(result["success"])
        self.assertEqual(json.loads(result["contentItems"][1]["text"]), {"ready": False})


if __name__ == "__main__":
    unittest.main()
