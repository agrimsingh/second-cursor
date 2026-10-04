"""Cursor bridge protocol tests; no provider request or desktop interaction."""
import base64
import copy
import json
import os
import pathlib
import signal
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from benchmarks import cursor_model_host as host
from benchmarks.model_host import HostConfig
from benchmarks.engines import ScopeError

VERSION = "cursor-sdk 1.0.35 node v22.23.1"
PARAMS = {"reasoning_effort": "medium", "fast": "true", "context": "256k"}
MODEL = {"id": "grok-4.7", "params": [{"id": key, "value": value} for key, value in PARAMS.items()]}
TOOL = {"name": "fixture_probe", "description": "Synthetic fixture only.",
        "inputSchema": {"type": "object", "properties": {}}}


def ready(**changes):
    event = {"type": "ready", "model": copy.deepcopy(MODEL), "version": VERSION,
             "isolation": {"verified": True, "fixture_tools": ["fixture_probe"],
                           "non_fixture_tools": [], "inherited_mcp_tool_count": 0,
                           "workspace_roots": [], "environment_access": False,
                           "setting_sources": [], "tools": ["mcp_tool_call", "get_mcp_tools_tool_call"],
                           "model_host": "cursor-sdk", "model_params": dict(PARAMS)}}
    event.update(changes)
    return event


def call(call_id="call-1", name="fixture_probe", arguments=None):
    return {"type": "call", "call_id": call_id, "name": name,
            "arguments": {"value": 1} if arguments is None else arguments}


def finished(**changes):
    event = {"type": "result", "status": "finished", "model": copy.deepcopy(MODEL),
             "usage": {"inputTokens": 9, "outputTokens": 3, "cacheReadTokens": 2,
                       "cacheWriteTokens": 1, "totalTokens": 15}, "result": "Finished."}
    event.update(changes)
    return event


class FakeBridge:
    def __init__(self, events, cleanup_status="cleaned"):
        self.events = list(events)
        self.sent = []
        self.closed = False
        self.cleanup_status = cleanup_status

    def send(self, value):
        self.sent.append(copy.deepcopy(value))
        if value.get("type") == "init":
            directory = pathlib.Path(value["store_dir"])
            directory.mkdir()
            (directory / "synthetic-private-transcript").write_text("disposable")

    def receive(self, deadline):
        if not self.events:
            raise host.HostError("Cursor bridge closed before trial completion")
        value = self.events.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def close(self):
        self.closed = True
        return {"status": self.cleanup_status}

    def finish(self, deadline):
        if self.events:
            raise host.IsolationError("Cursor sent protocol activity after its terminal result")


class CursorHostTests(unittest.TestCase):
    def run_host(self, events, execute=None, config=None, cleanup_status="cleaned", model_params=None):
        config = config or HostConfig("grok-4.7", "medium", 3, 2, VERSION)
        bridge = FakeBridge(events, cleanup_status)
        emitted, directories = [], []

        def factory(args, cwd, env):
            directories.append(cwd)
            self.assertTrue(pathlib.Path(cwd).is_dir())
            self.assertEqual(list(pathlib.Path(cwd).iterdir()), [])
            data_dir = pathlib.Path(env["CURSOR_DATA_DIR"])
            self.assertEqual(data_dir.parent, pathlib.Path(cwd).parent)
            transcript = data_dir / "projects" / "synthetic-owned-trial" / "agent-transcripts"
            transcript.mkdir(parents=True)
            (transcript / "synthetic-transcript.jsonl").write_text("disposable")
            return bridge

        with mock.patch.object(host, "host_version", return_value=VERSION), \
             mock.patch.object(host, "_Bridge", side_effect=factory):
            result = host.CursorModelHost(config, PARAMS if model_params is None else model_params).run("Complete synthetic fixture.", [TOOL],
                execute or (lambda name, args: {"content": [{"type": "text", "text": "ok"}]}), emitted.append)
        self.assertTrue(bridge.closed)
        self.assertTrue(all(not pathlib.Path(path).exists() for path in directories))
        self.assertTrue(all(not pathlib.Path(path).parent.exists() for path in directories))
        return result, bridge, emitted

    def test_complete_round_trip_preserves_mcp_content_and_cursor_usage(self):
        png = base64.b64encode(b"synthetic image transport").decode()
        response = {"content": [{"type": "text", "text": "state"},
                                {"type": "image", "mimeType": "image/png", "data": png}],
                    "structuredContent": {"checked": True}, "isError": False}
        result, bridge, events = self.run_host([ready(), call(), finished()], lambda name, args: response)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.final_response, "Finished.")
        self.assertEqual(result.model, "grok-4.7")
        self.assertEqual(result.tool_calls, 1)
        self.assertIsNone(result.model_ms)
        self.assertTrue(result.isolation["verified"])
        self.assertEqual(result.usage["source"], "cursor-sdk")
        self.assertEqual(result.usage["totalTokens"], 15)
        self.assertEqual(next(item["result"] for item in bridge.sent if item["type"] == "tool_result"), response)
        self.assertEqual(bridge.sent[0]["model"], MODEL)
        self.assertEqual(bridge.sent[0]["tools"], [TOOL])
        self.assertEqual(sum(event["type"] == "tool" for event in events), 1)

    def test_model_and_params_attested_before_actuator_and_at_finish(self):
        wrong_models = ["grok-4.7", {"id": "fallback", "params": MODEL["params"]},
                        {"id": "grok-4.7", "params": []},
                        {"id": "grok-4.7", "params": [{"id": key, "value": "false" if key == "fast" else value}
                                                               for key, value in PARAMS.items()]}]
        for model in wrong_models:
            for phase in ("ready", "result"):
                with self.subTest(model=model, phase=phase):
                    callback = mock.Mock(return_value="ok")
                    events = [ready(model=model), call(), finished()] if phase == "ready" else [ready(), finished(model=model)]
                    result, _, _ = self.run_host(events, callback)
                    self.assertEqual(result.status, "isolation_error")
                    self.assertFalse(result.isolation.get("verified"))
                    callback.assert_not_called()

    def test_every_containment_claim_is_required_before_actuation(self):
        changes = [{"verified": False}, {"fixture_tools": ["fixture_probe", "shell"]},
                   {"non_fixture_tools": ["shell"]}, {"inherited_mcp_tool_count": 1},
                   {"workspace_roots": ["/repo"]}, {"environment_access": True},
                   {"setting_sources": ["user"]}, {"tools": ["shell", "mcp"]},
                   {"model_host": "different"}, {"model_params": {"fast": "true"}}]
        for change in changes:
            with self.subTest(change=change):
                event = ready()
                event["isolation"].update(change)
                callback = mock.Mock()
                result, _, _ = self.run_host([event, call(), finished()], callback)
                self.assertEqual(result.status, "isolation_error")
                callback.assert_not_called()
        event = ready()
        del event["isolation"]["setting_sources"]
        result, _, _ = self.run_host([event, finished()])
        self.assertEqual(result.status, "isolation_error")

    def test_ready_version_drift_prevents_callback(self):
        callback = mock.Mock()
        result, _, _ = self.run_host([ready(version=VERSION + "other"), call()], callback)
        self.assertEqual(result.status, "isolation_error")
        callback.assert_not_called()

    def test_unknown_and_duplicate_requests_count_attempts_but_do_not_repeat_action(self):
        cases = [([call()], 0, 1), ([ready(), call(name="shell")], 0, 1),
                 ([ready(), call(call_id="")], 0, 1), ([ready(), call(), call()], 1, 2),
                 ([ready(), call(arguments=[])], 0, 1),
                 ([ready(), call(arguments={"value": float("nan")})], 0, 1)]
        for events, executed, attempts in cases:
            with self.subTest(events=events):
                callback = mock.Mock(return_value="ok")
                result, _, _ = self.run_host(events + [finished()], callback)
                self.assertIn(result.status, {"isolation_error", "failed"})
                self.assertEqual(callback.call_count, executed)
                self.assertEqual(result.tool_calls, attempts)

    def test_exhausted_budget_rejects_next_attempt(self):
        callback = mock.Mock(return_value="ok")
        config = HostConfig("grok-4.7", "medium", 1, 2, VERSION)
        result, _, _ = self.run_host([ready(), call(), call("call-2"), finished()], callback, config)
        self.assertEqual(result.status, "step_limit")
        self.assertEqual(result.tool_calls, 2)
        self.assertEqual(callback.call_count, 1)

    def test_protocol_ordering_unknown_events_and_early_result_fail(self):
        for events in ([finished()], [ready(), ready()], [ready(), {"type": "approval"}],
                       [ready(), {"type": "event", "event": {"type": "tool_call", "name": "shell"}}],
                       [ready(), {"type": "event", "event": {"type": "error", "message": "private-value"}}],
                       [ready(), []], [ready()]):
            with self.subTest(events=events):
                result, _, emitted = self.run_host(events)
                self.assertNotEqual(result.status, "completed")
                self.assertNotIn("private-value", json.dumps(emitted) + str(result.error))

    def test_callback_error_is_observed_without_exception_secrets(self):
        def fail(name, args):
            raise RuntimeError("Authorization: super-secret-token")
        result, bridge, events = self.run_host([ready(), call(), finished()], fail)
        self.assertEqual(result.status, "completed")
        response = next(item["result"] for item in bridge.sent if item["type"] == "tool_result")
        self.assertTrue(response["isError"])
        self.assertEqual(next(event["status"] for event in events if event["type"] == "tool"), "error")
        self.assertNotIn("super-secret-token", json.dumps(bridge.sent) + json.dumps(events) + json.dumps(result.as_dict()))

    def test_scope_refusal_is_actionable_and_model_can_recover(self):
        def callback(name, args):
            if args.get("value") == 1:
                raise ScopeError("Required tool arguments are missing: element_index")
            return {"content": [{"type": "text", "text": "Recovered."}]}
        result, bridge, events = self.run_host([ready(), call(), call("call-2", arguments={"value": 2}), finished()], callback)
        self.assertEqual(result.status, "completed")
        replies = [item["result"] for item in bridge.sent if item["type"] == "tool_result"]
        self.assertTrue(replies[0]["isError"])
        self.assertIn("Required tool arguments are missing: element_index", replies[0]["content"][0]["text"])
        self.assertEqual([event["status"] for event in events if event["type"] == "tool"], ["error", "completed"])

    def test_invalid_fixture_content_is_explicit_error(self):
        responses = [{"content": [{"type": "audio", "data": "xx"}]},
                     {"content": [{"type": "image", "mimeType": "image/png", "data": "notbase64"}]},
                     {"content": [], "structuredContent": {"bad": float("inf")}}]
        for response in responses:
            result, bridge, _ = self.run_host([ready(), call(), finished()], lambda name, args: response)
            observation = next(item["result"] for item in bridge.sent if item["type"] == "tool_result")
            self.assertTrue(observation["isError"])
            self.assertEqual(result.tool_calls, 1)

    def test_embedded_text_resource_is_normalized_for_cursor_custom_tools(self):
        response = {"content": [{"type": "text", "text": "state"},
                                {"type": "resource", "resource": {"uri": "fixture://state",
                                                                    "text": "embedded fixture observation"}}],
                    "structuredContent": {"checked": True}, "isError": False}
        result, bridge, _ = self.run_host([ready(), call(), finished()], lambda name, args: response)
        normalized = next(item["result"] for item in bridge.sent if item["type"] == "tool_result")
        self.assertEqual(result.status, "completed")
        self.assertEqual(normalized, {"content": [{"type": "text", "text": "state"},
                                                   {"type": "text", "text": "embedded fixture observation"}],
                                       "structuredContent": {"checked": True}, "isError": False})
        self.assertEqual(response["content"][1]["type"], "resource")

    def test_callback_deadline_restores_alarm_and_cleans_workspace(self):
        previous = signal.getsignal(signal.SIGALRM)
        config = HostConfig("grok-4.7", "medium", 2, .025, VERSION)
        result, _, events = self.run_host([ready(), call(), finished()], lambda name, args: time.sleep(2), config)
        self.assertEqual(result.status, "timeout")
        self.assertLess(result.wall_ms, 1000)
        self.assertEqual(signal.getsignal(signal.SIGALRM), previous)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)
        self.assertEqual(next(event["status"] for event in events if event["type"] == "tool"), "timeout")

    def test_sdk_failure_does_not_leak_error(self):
        for status in ("error", "cancelled", "nonsense"):
            result, _, events = self.run_host([ready(), finished(status=status, error="secret sdk URL")])
            self.assertNotEqual(result.status, "completed")
            self.assertNotIn("secret sdk URL", str(result.error) + json.dumps(events))

    def test_contradictory_result_and_activity_after_result_fail(self):
        for events in ([ready(), finished(error="private sdk error")],
                       [ready(), finished(), call()], [ready(), finished(), finished()]):
            result, _, emitted = self.run_host(events)
            self.assertNotEqual(result.status, "completed")
            self.assertNotIn("private sdk error", str(result.error) + json.dumps(emitted))

    def test_final_response_observed_before_terminal_cleanup_and_emitted_once(self):
        final_event = {"type": "event", "event": {"type": "final_response", "text": "Finished."}}
        result, _, emitted = self.run_host([ready(), final_event, finished()])
        self.assertEqual(result.status, "completed")
        self.assertEqual(sum(event.get("phase") == "final_answer" for event in emitted), 1)
        self.assertLessEqual(result.milestones_ms["final_response_completed"], result.milestones_ms["turn_completed"])
        for events in ([ready(), final_event, final_event, finished()],
                       [ready(), final_event, finished(result="Different.")]):
            result, _, _ = self.run_host(events)
            self.assertEqual(result.status, "failed")

    def test_cleanup_failure_invalidates_success(self):
        result, _, _ = self.run_host([ready(), finished()], cleanup_status="failed")
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.isolation["cleanup"]["status"], "failed")

    def test_events_and_milestones_use_host_monotonic_clock(self):
        events = [{"type": "event", "event": {"type": "activity"}},
                  {"type": "event", "event": {"type": "assistant", "text": "Working.", "api_key": "secret"}},
                  {"type": "event", "event": {"type": "usage", "usage": {"inputTokens": 9}}},
                  {"type": "event", "event": {"type": "status", "status": "running"}}]
        result, _, emitted = self.run_host([ready(), *events, call(), finished()])
        self.assertEqual(result.status, "completed")
        self.assertNotIn("secret", json.dumps(emitted))
        stamps = [event["at_ms"] for event in emitted if "at_ms" in event]
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(result.milestones_ms["host_started"], 0)
        self.assertEqual(result.milestones_ms["host_finished"], result.wall_ms)
        self.assertLessEqual(result.milestones_ms["first_model_activity"],
                             next(event["start_ms"] for event in emitted if event["type"] == "tool"))
        self.assertTrue(all("verified" not in name for name in result.milestones_ms))

    def test_bad_usage_does_not_create_billable_estimates(self):
        for usage in ({"inputTokens": -1}, {"outputTokens": True}, {"totalTokens": float("inf")},
                      {"api_key": "secret"}):
            result, _, events = self.run_host([ready(), finished(usage=usage)])
            self.assertEqual(result.status, "failed")
            self.assertNotIn("secret", json.dumps(events) + str(result.error))

    def test_constructor_checks_reasoning_and_param_types(self):
        config = HostConfig("grok-4.7", "medium", 1, 2, VERSION)
        for params in ({}, {**PARAMS, "reasoning_effort": "high"}, {**PARAMS, "fast": True},
                       {**PARAMS, "effort": "medium"}, {"effort": "high", "fast": "true"}):
            with self.assertRaises(ValueError):
                host.CursorModelHost(config, params)

    def test_grok_45_uses_catalog_effort_alias_without_rewriting_parameters(self):
        for effort in ("low", "medium", "high"):
            with self.subTest(effort=effort):
                params = {"effort": effort, "fast": "true"}
                model = {"id": "grok-4.5", "params": [{"id": key, "value": value} for key, value in params.items()]}
                config = HostConfig("grok-4.5", effort, 3, 2, VERSION)
                attestation = ready(model=model)
                attestation["isolation"]["model_params"] = dict(params)
                result, bridge, _ = self.run_host([attestation, call(), finished(model=model)],
                                                  config=config, model_params=params)
                self.assertEqual(result.status, "completed")
                self.assertEqual(result.reasoning_effort, effort)
                self.assertEqual(result.isolation["model_params"], params)
                self.assertEqual(bridge.sent[0]["model"], model)

    def test_child_environment_does_not_inherit_unrelated_credentials_or_node_hooks(self):
        with mock.patch.dict(os.environ, {"CURSOR_API_KEY": "cursor-private", "AWS_SECRET_ACCESS_KEY": "aws-private",
                                         "NODE_OPTIONS": "--require unsafe.js", "TEST_SECRET": "private",
                                         "CURSOR_DATA_DIR": "/some-unowned-store"}, clear=True):
            env = host._child_environment()
        self.assertEqual(env["CURSOR_API_KEY"], "cursor-private")
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertNotIn("NODE_OPTIONS", env)
        self.assertNotIn("TEST_SECRET", env)
        self.assertNotIn("CURSOR_DATA_DIR", env)


class ActualBridgeTransportTests(unittest.TestCase):
    def make_bridge(self, source):
        temp = tempfile.TemporaryDirectory(prefix="sc-fake-cursor-bridge-")
        self.addCleanup(temp.cleanup)
        script = pathlib.Path(temp.name) / "fake.py"
        script.write_text(source)
        bridge = host._Bridge([sys.executable, "-u", str(script)], temp.name, host._child_environment())
        self.addCleanup(bridge.close)
        return bridge

    def test_malformed_stdout_never_echoes_source(self):
        bridge = self.make_bridge("print('private-secret-not-json', flush=True)\n")
        with self.assertRaises(host.HostError) as error:
            bridge.receive(time.monotonic() + 2)
        self.assertNotIn("private-secret", str(error.exception))

    def test_stalled_stdout_respects_deadline_and_kills_child(self):
        bridge = self.make_bridge("import time\ntime.sleep(20)\n")
        with self.assertRaises(host.TrialDeadline):
            bridge.receive(time.monotonic() + .025)
        evidence = bridge.close()
        self.assertIn(evidence["status"], {"cleaned", "no_live_members"})
        self.assertFalse(bridge.process.group_reserved)

    def test_nonfinite_protocol_and_nonobject_rejected(self):
        for value in ('[]', '{"type":"event","value":NaN}'):
            bridge = self.make_bridge(f"print({value!r}, flush=True)\n")
            with self.assertRaises(host.HostError):
                bridge.receive(time.monotonic() + 2)
            bridge.close()

    def test_child_that_outlives_bridge_leader_is_killed_before_reaping(self):
        source = """import json, subprocess, sys
child = subprocess.Popen([sys.executable, '-u', '-c',
    'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print("ready",flush=True);time.sleep(20)'],
    stdout=subprocess.PIPE)
child.stdout.readline()
print(json.dumps({'pid':child.pid}), flush=True)
"""
        bridge = self.make_bridge(source)
        child = bridge.receive(time.monotonic() + 2)["pid"]
        # EOF from the short-lived leader must not release process-group identity.
        with self.assertRaises(host.HostError):
            bridge.receive(time.monotonic() + 2)
        self.assertTrue(bridge.process.group_reserved)
        evidence = bridge.close()
        self.assertEqual(evidence["status"], "cleaned")
        self.assertIn(int(signal.SIGKILL), evidence["signals"])
        self.assertTrue(any(row["pid"] == child for rows in evidence["snapshots"] for row in rows))
        self.assertFalse(any(not row["state"].startswith("Z") for row in evidence["snapshots"][-1]))
        self.assertFalse(bridge.process.group_reserved)

    def test_oversized_protocol_line_is_rejected_without_echo(self):
        with mock.patch.object(host, "MAX_PROTOCOL_LINE_BYTES", 64):
            bridge = self.make_bridge("print('private-' * 100, flush=True)\n")
            with self.assertRaises(host.HostError) as error:
                bridge.receive(time.monotonic() + 2)
            self.assertNotIn("private-", str(error.exception))
            bridge.close()

    def test_finish_rejects_post_result_messages_and_nonzero_exit(self):
        for source in ("print('{}', flush=True)\n", "raise SystemExit(1)\n"):
            bridge = self.make_bridge(source)
            with self.assertRaises(host.HostError):
                bridge.finish(time.monotonic() + 2)
            bridge.close()
        bridge = self.make_bridge("pass\n")
        bridge.finish(time.monotonic() + 2)
        bridge.close()


if __name__ == "__main__":
    unittest.main()
