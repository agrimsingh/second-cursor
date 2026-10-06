#!/usr/bin/env python3
"""Run pinned, unmodified Arc browser settling against the shared local fixtures.

This is a deterministic driver comparison, not a model benchmark. It never
attaches to an existing browser or uses the macOS input backend. See
arc_reference.md for setup, timing boundaries, and comparison limitations.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode, urlparse

import run

ROOT = Path(__file__).resolve().parents[1]
ARC_COMMIT = "6ca19d62c95106732fad28f488ecd458c08e02f4"
ARC_REPOSITORY = "https://github.com/shhivv/arc-cua"


def dependency_identity():
    """Require a verifiable pinned install; never silently benchmark another Arc."""
    distribution = importlib.metadata.distribution("arc-cua")
    source = json.loads(distribution.read_text("direct_url.json") or "{}")
    actual = source.get("vcs_info", {}).get("commit_id")
    if actual != ARC_COMMIT:
        raise RuntimeError(f"Arc install must be pinned to {ARC_COMMIT}; installed source is {source}")
    files = {}
    for name in ("arc_cua/backends/chrome.py", "arc_cua/backends/chrome_page.js",
                 "arc_cua/runtime.py", "arc_cua/settling.py"):
        files[name] = hashlib.sha256(Path(distribution.locate_file(name)).read_bytes()).hexdigest()
    return {
        "repository": ARC_REPOSITORY, "commit": actual, "version": distribution.version,
        "source_files_sha256": files, "python": sys.version, "platform": platform.platform(),
        "dependencies": {name: importlib.metadata.version(name) for name in ("httpx", "websockets")},
    }


def fixture_url(base_url, case, trial):
    """Only our locally owned fixture server is a valid action destination."""
    parsed = urlparse(base_url)
    if (parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port
            or parsed.username or parsed.password or parsed.path not in ("", "/")
            or parsed.query or parsed.fragment):
        raise ValueError("Arc reference accepts only its own loopback HTTP fixture server")
    return base_url.rstrip("/") + "/arc-settling.html?" + urlencode({"case": case, "trial": trial})


class OwnedBrowser:
    """Harness-owned process and profile using SC launch flags minus its extension.

    Arc's launcher adds background-timer overrides. We deliberately use the
    shared SC launch command instead so these flags cannot favor the reference.
    The profile and process group are created here and are the only cleanup targets.
    """

    def __init__(self, *, headed=True):
        launcher = run.load_start_browser()
        self.identity = launcher.browser_identity()
        self.profile = Path(tempfile.mkdtemp(prefix="second-cursor-arc-reference-"))
        self.headed = headed
        self.process = None
        self.command = [arg for arg in launcher.launch_command(self.identity["binary"], self.profile,
                                                              headed=headed)
                        if not arg.startswith(("--load-extension=", "--disable-extensions-except="))]
        try:
            with (self.profile / "browser.log").open("w") as log:
                self.process = subprocess.Popen(self.command, stdout=log, stderr=log, start_new_session=True)
            self.pid = self.process.pid
            deadline = time.monotonic() + 20
            port_file = self.profile / "DevToolsActivePort"
            while True:
                if self.process.poll() is not None:
                    raise RuntimeError(f"Owned Chrome exited during startup ({self.process.returncode})")
                lines = port_file.read_text().split() if port_file.exists() else []
                if len(lines) >= 2:
                    self.ws_url = f"ws://127.0.0.1:{lines[0]}{lines[1]}"
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError("Owned Chrome did not expose its DevTools endpoint")
                time.sleep(0.05)
        except BaseException:
            self.stop()
            raise

    @property
    def metadata(self):
        return {"identity": self.identity, "headed": self.headed,
                "launch_flags": [arg for arg in self.command[1:] if not arg.startswith("--user-data-dir=")],
                "profile": "fresh harness-owned temporary directory",
                "extension_loaded": False}

    def stop(self, log_destination=None):
        if self.process is not None:
            for sig, timeout in ((signal.SIGTERM, 5), (signal.SIGKILL, 2)):
                try:
                    os.killpg(self.process.pid, sig)
                    self.process.wait(timeout=timeout)
                except ProcessLookupError:
                    break
                except subprocess.TimeoutExpired:
                    continue
            self.process = None
        if log_destination and (self.profile / "browser.log").exists():
            shutil.copy(self.profile / "browser.log", log_destination)
        shutil.rmtree(self.profile, ignore_errors=True)


def attach_owned_browser(browser):
    from arc_cua.backends.cdp import CDPConnection
    from arc_cua.backends.chrome import ChromeBackend

    connection = CDPConnection(browser.ws_url)
    try:
        pages = [target for target in connection.call("Target.getTargets")["targetInfos"]
                 if target["type"] == "page"]
        if len(pages) != 1 or pages[0]["url"] != "about:blank":
            raise RuntimeError("Owned Chrome must contain exactly its initial blank tab")
        # Second Cursor opens its agent tab in the background. Keep the owned
        # blank tab active and create the same background target for Arc.
        target_id = connection.call("Target.createTarget", {"url": "about:blank", "background": True})["targetId"]
        return ChromeBackend(connection, target_id, owns_target=True)
    except BaseException:
        connection.close()
        raise


class ClickOnce:
    """A fixture script selecting by observed accessible name; no service/model."""

    def __init__(self, name):
        self.name, self.started = name, None

    def decide(self, *, subtask, snapshot, history):
        from arc_cua import ActionKind, Decision, TerminalKind

        if history:
            return Decision(terminal=TerminalKind.SUBTASK_COMPLETE)
        targets = [element for element in snapshot.elements
                   if element.name == self.name and element.enabled and element.visible
                   and ActionKind.CLICK in element.actions]
        if len(targets) != 1:
            raise RuntimeError(f"Expected one actionable {self.name!r}; found {len(targets)}")
        self.started = time.monotonic()
        return Decision(kind=ActionKind.CLICK, target_id=targets[0].id)


def click_once(backend, name):
    from arc_cua import DesktopExecutor, RuntimeConfig, Subtask

    policy = ClickOnce(name)
    executor = DesktopExecutor(backend, policy, config=RuntimeConfig())
    started = time.monotonic()
    result = executor.run(Subtask(goal=f"Click the local fixture's {name} button once",
                                 verification=("The independent fixture observer judges the resulting effect",),
                                 max_actions=2))
    returned = time.monotonic()
    if len(result.history) != 1:
        raise RuntimeError(f"Expected exactly one executed action; got {len(result.history)} ({result.reason})")
    return result, policy.started, returned, started


def setup_click(backend, name):
    """One guarded setup action without waiting for its deliberately pending effect."""
    from arc_cua import Subtask
    from arc_cua.validation import materialize_action

    snapshot = backend.observe()
    task = Subtask(goal=f"Prepare the local fixture with {name}",
                   verification=("The independent fixture server records the setup request",), max_actions=1)
    decision = ClickOnce(name).decide(subtask=task, snapshot=snapshot, history=[])
    # The upstream materializer copies the observed semantic guard. Constructing
    # an ExecutableAction with only an id produces an empty guard and is always
    # rejected by ChromeBackend, even when the page has not changed.
    action = materialize_action(decision, snapshot, task)
    backend.execute(snapshot, action)


def run_case(browser, backend, fixture, case, repeat):
    from arc_settling import BROWSER_CASES, VisibleWatcher, timing_summary

    spec = BROWSER_CASES[case]
    if spec.get("observe") == "screenshot":
        return {"driver": "arc-cua", "surface": "browser", "case": case, "repeat": repeat,
                "unsupported": "Arc returns DOM plus optional PNG; no equivalent screenshot-only action observation"}
    url = fixture_url(fixture.base_url, case, repeat)
    backend.navigate(url)
    if spec.get("longpoll"):
        # Setup is execute-only in both drivers: waiting for this deliberately
        # unrelated 30-second request would contaminate the measured action.
        setup_click(backend, "Start background polling")
        if not fixture.wait_longpoll(case):
            raise RuntimeError("The unrelated longpoll did not start before the measured action")
    observer = VisibleWatcher(run.page_socket(browser, url))
    observer.start()
    try:
        result, started, returned, observation_started = click_once(backend, "Run action")
        # Retain premature-return evidence; observing a later effect cannot turn
        # the original returned snapshot into a successful settled observation.
        deadline = returned + (0.1 if not spec["expected_effect"] else 6)
        while observer.first_visible_at is None and not observer.error and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        observer.stop()
    marker = f"Effect ready: {case}"
    snapshot = result.final_snapshot
    compact = [element.compact() for element in snapshot.elements]
    observed_effect = any(marker in str(element.get("name", "")) or marker in str(element.get("value", ""))
                          for element in compact)
    return {
        "driver": "arc-cua", "surface": "browser", "case": case, "repeat": repeat,
        "expected_effect": spec["expected_effect"],
        **timing_summary(started, returned, observer),
        "observe_act_observe_ms": round((returned - observation_started) * 1000, 3),
        "ready_in_result": observed_effect,
        "action_count": len(result.history), "runtime_status": result.status.value,
        "runtime_status_is_independent_success_proof": False,
        "checks": {
            "observer_healthy": observer.error is None,
            "one_fixture_action": bool(observer.samples) and observer.samples[-1]["action_count"] == 1,
            "bounded_return": (returned - started) * 1000 <= spec["max_return_ms"],
            "effect_matches_contract": (observer.first_visible_at is not None) == spec["expected_effect"],
            "no_false_ready": spec["expected_effect"] or not observed_effect,
        },
        "returned_snapshot": compact, "observer_samples": observer.samples, "requests": fixture.requests,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", action="store_true", help="launch the isolated test browser and execute fixtures")
    parser.add_argument("--case", action="append", dest="cases", help="repeat to select fixture cases")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--headless", action="store_true", help="diagnostic only; not comparable to headed runs")
    parser.add_argument("--output", type=Path, help="artifact directory")
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    source = dependency_identity()
    if not args.run:
        print(json.dumps({"source": source, "browser": run.load_start_browser().browser_identity(),
                          "status": "ready; --run is required to launch fixtures"}, indent=2))
        return 0

    from arc_cua import RuntimeConfig
    from arc_settling import ArcFixtureServer, BROWSER_CASES, VERSION, summarize

    cases = args.cases or list(BROWSER_CASES)
    unknown = set(cases) - set(BROWSER_CASES)
    if unknown:
        parser.error(f"unknown fixture cases: {sorted(unknown)}")
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output = args.output or ROOT / "e2e/artifacts" / f"arc-reference-{timestamp}"
    output.mkdir(parents=True, exist_ok=False)
    manifest = {"driver": "arc-cua", "source": source, "cases": cases, "repeats": args.repeats,
                "fixture_hashes": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                                   for path in (ROOT / "e2e/arc_settling.py", ROOT / "e2e/fixtures/arc-settling.html")},
                "runtime_config": dataclasses.asdict(RuntimeConfig()),
                "browser_settle_timeout_s": 10.0,
                "excluded": ["browser launch", "fixture navigation", "background polling setup",
                             "initial target observation (also reported in observe_act_observe_ms)"],
                "limitations": ["No model; deterministic observed-name selection",
                                "DOM-visible observer is not compositor presentation proof",
                                "Arc direct Python API versus SC MCP and extension transport",
                                "Arc backend uses focus emulation; browser process flags match SC excluding extension",
                                "Native backends and coexistence are outside this runner"]}
    results, browser, backend = [], None, None
    try:
        browser = OwnedBrowser(headed=not args.headless)
        manifest["browser"] = browser.metadata
        backend = attach_owned_browser(browser)
        manifest["browser_settle_timeout_s"] = backend.settle_timeout_s
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        for repeat in range(1, args.repeats + 1):
            for case in cases:
                with ArcFixtureServer() as fixture:
                    try:
                        result = run_case(browser, backend, fixture, case, repeat)
                    except Exception as exc:
                        result = {"driver": "arc-cua", "case": case, "repeat": repeat,
                                  "error": f"{type(exc).__name__}: {exc}"}
                    results.append(result)
                    (output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
                    rows = [row for row in results if "return_ms" in row]
                    (output / "arc-settling.json").write_text(json.dumps({
                        "version": VERSION, "label": "arc-reference", "kind": "deterministic upstream driver probes",
                        "rows": rows, "summary": summarize(rows), "results": results,
                        "source_revision": source["commit"], "chrome_identity": browser.identity,
                        "fixture_hashes": manifest["fixture_hashes"],
                    }, indent=2) + "\n")
                    print(json.dumps({key: value for key, value in result.items()
                                      if key not in ("observer_samples", "returned_snapshot", "requests")}), flush=True)
    finally:
        if backend is not None:
            backend.close()
        if browser is not None:
            browser.stop(output / "browser.log")
    print(json.dumps({"artifacts": str(output), "trials": len(results)}))
    return int(any("error" in result or result.get("observer_error")
                   or not all(result.get("checks", {}).values()) for result in results))


if __name__ == "__main__":
    raise SystemExit(main())
