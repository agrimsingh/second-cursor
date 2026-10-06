"""Deterministic isolation/deadline tests; no browser, model, or native input."""
import contextlib
import importlib.util
import io
import sys
import types
import unittest
from unittest.mock import Mock, patch

import browser_expect_reference as reference


class FakeClock:
    def __init__(self):
        self.value = 0

    def now(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


@unittest.skipUnless(importlib.util.find_spec("arc_cua"), "requires the optional pinned Arc environment")
class ConfirmPolicyTests(unittest.TestCase):
    def setUp(self):
        from arc_cua import ActionKind, DesktopElement, DesktopSnapshot
        self.clock = FakeClock()
        self.target = DesktopElement(id="w1", role="button", name="Save draft", source="chrome_dom",
                                     actions=(ActionKind.CLICK,))
        self.pending = DesktopSnapshot(application="Chrome", window="Fixture", revision="r1",
                                       elements=(self.target,))
        self.ready = DesktopSnapshot(application="Chrome", window="Fixture", revision="r2", elements=(
            self.target, DesktopElement(id="w2", role="status", name="Draft saved once", source="chrome_dom")))

    def backend(self, snapshots, *, read_seconds=0.01):
        backend = Mock()
        snapshots = iter(snapshots)
        def observe():
            self.clock.advance(read_seconds)
            return next(snapshots)
        backend.observe.side_effect = observe
        backend.execute.side_effect = lambda *_: self.clock.advance(0.005)
        return backend

    def invoke(self, backend, **kwargs):
        return reference.act_and_confirm(backend, "Save draft", "Draft saved once", clock=self.clock.now,
                                         sleep=self.clock.advance, **kwargs)

    def test_one_mutation_then_all_confirmation_reads_are_counted(self):
        backend = self.backend([self.pending, self.pending, self.pending, self.ready])
        result = self.invoke(backend)
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["call_counts"], {"initial_observations": 1, "mutations": 1,
                         "confirmation_observations": 3, "total_backend_calls": 5, "model_calls": 0})
        self.assertEqual([r["expected_text_present"] for r in result["confirmation_reads"]], [False, False, True])
        self.assertAlmostEqual(result["return_ms"], 235)
        self.assertAlmostEqual(result["observe_act_confirm_ms"], 245)
        observed, action = backend.execute.call_args.args
        self.assertIs(observed, self.pending)
        self.assertEqual(action.target_guard, self.target.semantic_guard())
        self.assertTrue(action.target_guard)
        backend.execute.assert_called_once()

    def test_timeout_never_repeats_the_mutation(self):
        backend = self.backend([self.pending] * 20)
        result = self.invoke(backend, timeout_ms=250)
        self.assertFalse(result["confirmed"])
        self.assertFalse(result["expected_text_in_final_snapshot"])
        self.assertGreaterEqual(result["return_ms"], 250)
        backend.execute.assert_called_once()

    def test_positive_observation_returned_after_deadline_is_not_accepted(self):
        backend = self.backend([self.pending, self.ready], read_seconds=0.5)
        result = self.invoke(backend, timeout_ms=250)
        self.assertFalse(result["confirmed"])
        self.assertTrue(result["expected_text_in_final_snapshot"])
        self.assertGreater(result["deadline_overrun_ms"], 0)
        backend.execute.assert_called_once()

    def test_failed_mutation_is_never_retried(self):
        backend = self.backend([self.pending])
        backend.execute.side_effect = RuntimeError("delivery uncertain")
        with self.assertRaisesRegex(RuntimeError, "delivery uncertain"):
            self.invoke(backend)
        backend.execute.assert_called_once()
        backend.observe.assert_called_once()

    def test_preexisting_completion_does_not_fake_success(self):
        backend = self.backend([self.ready])
        with self.assertRaisesRegex(RuntimeError, "already visible"):
            self.invoke(backend)
        backend.execute.assert_not_called()

    def test_ambiguous_target_is_rejected_before_mutation(self):
        from arc_cua import DesktopSnapshot
        ambiguous = DesktopSnapshot(application="Chrome", window="Fixture", revision="r1",
                                    elements=(self.target, self.target))
        backend = self.backend([ambiguous])
        with self.assertRaisesRegex(RuntimeError, "found 2"):
            self.invoke(backend)
        backend.execute.assert_not_called()


class ObserverEvidenceTests(unittest.TestCase):
    def test_hidden_text_does_not_satisfy_completion(self):
        snapshot = Mock(elements=[Mock(name="not-used")])
        snapshot.elements[0].name = "Draft saved once"
        snapshot.elements[0].visible = False
        self.assertFalse(reference.snapshot_has_text(snapshot, "Draft saved once"))

    def test_only_visible_element_name_is_used(self):
        element = Mock()
        element.name = "Waiting"
        element.value = "Draft saved once"
        element.visible = True
        self.assertFalse(reference.snapshot_has_text(Mock(elements=[element]), "Draft saved once"))
        element.name = "Draft saved once"
        self.assertTrue(reference.snapshot_has_text(Mock(elements=[element]), "Draft saved once"))


class ReferenceIsolationTests(unittest.TestCase):
    def test_preflight_never_launches_browser(self):
        launcher = Mock()
        launcher.browser_identity.return_value = {"version": "test"}
        with patch.object(sys, "argv", ["browser_expect_reference.py"]), \
                patch.object(reference, "loaded_arc_identity", return_value={"commit": "test"}), \
                patch.object(reference, "fixture_identity", return_value={"fixture": "hash"}), \
                patch.object(reference.run, "load_start_browser", return_value=launcher), \
                patch.object(reference.arc_reference, "OwnedBrowser") as browser, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(reference.main(), 0)
        browser.assert_not_called()

    def test_owned_fixture_url_rejects_external_origins(self):
        for origin in ("https://example.com", "http://localhost:1234", "http://127.0.0.1:1234/outside"):
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                reference.owned_fixture_url(types.SimpleNamespace(origin=origin, case="delayed_save"))
        self.assertEqual(reference.owned_fixture_url(types.SimpleNamespace(
            origin="http://127.0.0.1:1234", case="delayed_save")),
            "http://127.0.0.1:1234/browser-expect.html?case=delayed_save")

    def test_unconfirmed_modal_never_receives_a_confirmation_click(self):
        fixture = types.SimpleNamespace(origin="http://127.0.0.1:1234", case="delayed_modal",
            record_id="REC-4402", seed=402, delay_ms=1500,
            evidence=Mock(return_value={}), judge=Mock(return_value={"exact_commit_count": False}))
        stage = {"confirmed": False, "started_at": 1, "returned_at": 7.1, "return_ms": 6100,
            "observe_act_confirm_ms": 6120, "deadline_overrun_ms": 100,
            "call_counts": {"initial_observations": 1, "mutations": 1, "confirmation_observations": 5,
                            "total_backend_calls": 7, "model_calls": 0}}
        with patch.object(reference, "preflight", return_value={}), \
                patch.object(reference, "independent_ui_receipt", return_value={"visible": False}), \
                patch.object(reference, "act_and_confirm", return_value=stage) as action:
            result = reference.run_case(Mock(), Mock(), fixture)
        action.assert_called_once()
        self.assertEqual(action.call_args.args[1], "Save record")
        self.assertFalse(result["matched"])
        self.assertFalse(result["checks"]["exact_mutation_count"])


if __name__ == "__main__":
    unittest.main()
