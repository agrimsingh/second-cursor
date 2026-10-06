"""Isolation guarantees for the optional reference runner; no browser or Arc install needed."""
import contextlib
import io
import importlib.util
import sys
import types
import unittest
from unittest.mock import Mock, patch

import arc_reference as reference


class ReferenceIsolationTests(unittest.TestCase):
    @unittest.skipUnless(importlib.util.find_spec("arc_cua"), "requires the optional pinned Arc environment")
    def test_setup_click_preserves_upstream_semantic_guard(self):
        from arc_cua import ActionKind, DesktopElement, DesktopSnapshot
        element = DesktopElement(id="w3", role="button", name="Start background polling",
                                 actions=(ActionKind.CLICK,), source="chrome_dom")
        snapshot = DesktopSnapshot(application="Chrome", window="Fixture", revision="r1", elements=(element,))
        backend = Mock()
        backend.observe.return_value = snapshot
        reference.setup_click(backend, element.name)
        observed, action = backend.execute.call_args.args
        self.assertIs(observed, snapshot)
        self.assertEqual(action.target_id, element.id)
        self.assertEqual(action.target_guard, element.semantic_guard())
        self.assertTrue(action.target_guard)
        backend.execute.assert_called_once()

    def test_fixture_url_encodes_only_owned_loopback_destination(self):
        self.assertEqual(reference.fixture_url("http://127.0.0.1:4321", "slow_fetch", 2),
                         "http://127.0.0.1:4321/arc-settling.html?case=slow_fetch&trial=2")
        for base in ("https://example.com", "http://localhost:123", "http://127.0.0.1",
                     "http://127.0.0.1:123/other", "http://user@127.0.0.1:123",
                     "http://127.0.0.1:123/?outside=1", "http://127.0.0.1:123/#other"):
            with self.subTest(base=base), self.assertRaises(ValueError):
                reference.fixture_url(base, "immediate", 1)

    def test_wrong_or_unverifiable_install_is_refused_before_source_load(self):
        distribution = Mock()
        for direct_url in (None, '{}', '{"vcs_info":{"commit_id":"different"}}'):
            distribution.read_text.return_value = direct_url
            with self.subTest(direct_url=direct_url), \
                    patch.object(reference.importlib.metadata, "distribution", return_value=distribution), \
                    self.assertRaises(RuntimeError):
                reference.dependency_identity()
            distribution.locate_file.assert_not_called()

    def test_preflight_never_launches_browser(self):
        launcher = Mock()
        launcher.browser_identity.return_value = {"binary": "fake"}
        with patch.object(sys, "argv", ["arc_reference.py"]), \
                patch.object(reference, "dependency_identity", return_value={"commit": reference.ARC_COMMIT}), \
                patch.object(reference.run, "load_start_browser", return_value=launcher), \
                patch.object(reference, "OwnedBrowser") as browser, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(reference.main(), 0)
        browser.assert_not_called()

    def test_attaching_requires_sole_blank_owned_tab(self):
        connection = Mock()
        factory = Mock(return_value=connection)
        backend = Mock()
        modules = {"arc_cua.backends.cdp": types.SimpleNamespace(CDPConnection=factory),
                   "arc_cua.backends.chrome": types.SimpleNamespace(ChromeBackend=backend)}
        browser = types.SimpleNamespace(ws_url="ws://127.0.0.1:123/owned")
        for pages in ([], [{"type": "page", "url": "https://example.com", "targetId": "user"}],
                      [{"type": "page", "url": "about:blank", "targetId": "a"},
                       {"type": "page", "url": "about:blank", "targetId": "b"}]):
            connection.call.reset_mock()
            connection.call.return_value = {"targetInfos": pages}
            with self.subTest(pages=pages), patch.dict(sys.modules, modules), self.assertRaises(RuntimeError):
                reference.attach_owned_browser(browser)
            self.assertEqual(connection.call.call_count, 1)
            backend.assert_not_called()
        self.assertEqual(connection.close.call_count, 3)

    def test_fixture_tab_is_created_in_background_and_owned_by_backend(self):
        connection = Mock()
        connection.call.side_effect = [{"targetInfos": [{"type": "page", "url": "about:blank",
                                                          "targetId": "blank"}]}, {"targetId": "fixture"}]
        backend = Mock()
        modules = {"arc_cua.backends.cdp": types.SimpleNamespace(CDPConnection=Mock(return_value=connection)),
                   "arc_cua.backends.chrome": types.SimpleNamespace(ChromeBackend=backend)}
        with patch.dict(sys.modules, modules):
            reference.attach_owned_browser(types.SimpleNamespace(ws_url="ws://127.0.0.1:123/owned"))
        connection.call.assert_any_call("Target.createTarget", {"url": "about:blank", "background": True})
        backend.assert_called_once_with(connection, "fixture", owns_target=True)


if __name__ == "__main__":
    unittest.main()
