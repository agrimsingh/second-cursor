#!/usr/bin/env python3
"""Pinned Arc browser comparison using one guarded action and explicit observation polling.

No model, desktop input, user profile, or modified Arc backend is used. Completion
requires the shared fixture's expected UI plus its independent persisted ledger.
Run without --run to inspect the pinned dependency and browser without launching it.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import time
from urllib.parse import urlencode

import arc_reference
import run

ROOT = Path(__file__).resolve().parents[1]
VERSION = "browser-expect-reference/1"


def loaded_arc_identity():
    """Include every installed upstream Python/JS source used by this adapter."""
    identity = arc_reference.dependency_identity()
    distribution = importlib.metadata.distribution("arc-cua")
    spec = importlib.util.find_spec("arc_cua")
    expected_package = Path(distribution.locate_file("arc_cua/__init__.py")).resolve()
    if spec is None or spec.origin is None or Path(spec.origin).resolve() != expected_package:
        raise RuntimeError("Python would load an Arc package outside the pinned distribution")
    identity["loaded_package"] = str(expected_package)
    identity["all_package_source_sha256"] = {
        str(name): hashlib.sha256(Path(distribution.locate_file(name)).read_bytes()).hexdigest()
        for name in distribution.files or []
        if str(name).startswith("arc_cua/") and Path(name).suffix in (".py", ".js")
    }
    if not identity["all_package_source_sha256"]:
        raise RuntimeError("Pinned Arc install contains no fingerprintable package source")
    return identity


def snapshot_has_text(snapshot, expected_text):
    """Judge driver completion only from Arc's observable, visible text elements."""
    return any(element.visible and expected_text in str(element.name)
               for element in snapshot.elements)


def act_and_confirm(backend, action_name, expected_text, *, timeout_ms=5000,
                    poll_ms=100, clock=time.monotonic, sleep=time.sleep):
    """One materialized mutation, then reads only; never retry a failed mutation.

    This is a task-level adapter, not an invented upstream expect API. Full Arc
    observations account for every confirmation read, and a late successful read
    remains outside the deadline. An in-flight observe may exceed that deadline;
    its actual elapsed time is retained rather than described as hard-bounded.
    """
    from arc_cua import ActionKind, Decision, Subtask
    from arc_cua.validation import materialize_action

    if not action_name or not expected_text or timeout_ms <= 0 or poll_ms <= 0:
        raise ValueError("Action, expected text, and positive timing bounds are required")
    observation_started = clock()
    snapshot = backend.observe()
    if snapshot_has_text(snapshot, expected_text):
        raise RuntimeError("Fixture completion was already visible before its measured action")
    targets = [element for element in snapshot.elements
               if element.name == action_name and element.visible and element.enabled
               and ActionKind.CLICK in element.actions]
    if len(targets) != 1:
        raise RuntimeError(f"Expected one actionable {action_name!r}; found {len(targets)}")
    task = Subtask(goal=f"Click {action_name} once and wait for {expected_text}",
                   verification=(f"Visible text includes {expected_text}",), max_actions=1)
    decision = Decision(kind=ActionKind.CLICK, target_id=targets[0].id)
    action = materialize_action(decision, snapshot, task)
    started = clock()
    deadline = started + timeout_ms / 1000
    backend.execute(snapshot, action)
    action_returned = clock()
    reads = []
    confirmed = False
    while True:
        read_started = clock()
        final_snapshot = backend.observe()
        returned = clock()
        present = snapshot_has_text(final_snapshot, expected_text)
        reads.append({"started_ms": round((read_started - started) * 1000, 3),
                      "returned_ms": round((returned - started) * 1000, 3), "expected_text_present": present})
        confirmed = present and returned <= deadline
        if confirmed or returned >= deadline:
            break
        sleep(min(poll_ms / 1000, max(0, deadline - returned)))
    return {
        "started_at": started, "returned_at": returned,
        "return_ms": round((returned - started) * 1000, 3),
        "observe_act_confirm_ms": round((returned - observation_started) * 1000, 3),
        "action_return_ms": round((action_returned - started) * 1000, 3),
        "confirmed": confirmed, "expected_text_in_final_snapshot": present,
        "deadline_overrun_ms": round(max(0, returned - deadline) * 1000, 3),
        "timeout_ms": timeout_ms, "poll_ms": poll_ms,
        "call_counts": {"initial_observations": 1, "mutations": 1,
                        "confirmation_observations": len(reads), "total_backend_calls": 2 + len(reads),
                        "model_calls": 0},
        "confirmation_reads": reads, "returned_snapshot": final_snapshot.compact(),
        "initial_snapshot": snapshot.compact(),
    }


def fixture_identity():
    files = ("browser_expect_reference.py", "browser_expect.py", "fixtures/browser-expect.html",
             "arc_reference.py", "arc_settling.py", "run.py", "start-browser.py")
    return {"e2e/" + name: hashlib.sha256((ROOT / "e2e" / name).read_bytes()).hexdigest() for name in files}


def owned_fixture_url(fixture):
    # Reuse the existing runner's strict loopback-only origin validator; this
    # generated settling URL is discarded, and only the owned expect URL is used.
    arc_reference.fixture_url(fixture.origin, "ownership-validation", 0)
    return fixture.origin + "/browser-expect.html?" + urlencode({"case": fixture.case})


def preflight(backend, *, timeout_s=6):
    started, reads = time.monotonic(), 0
    while True:
        snapshot = backend.observe()
        reads += 1
        if snapshot_has_text(snapshot, "Revision "):
            return {"observations": reads, "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                    "context": dict(snapshot.context)}
        if time.monotonic() - started >= timeout_s:
            raise RuntimeError("Owned fixture did not load its visible Revision state")
        time.sleep(.05)


def visible_expression(expected_text):
    return """(() => {
      const visible = element => {
        if (!element || !element.getClientRects().length) return false;
        for (let node = element; node; node = node.parentElement) {
          const style = getComputedStyle(node);
          if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
        }
        return true;
      };
      const status = document.querySelector('#status');
      return {visible: visible(status) && status.textContent.includes(EXPECTED),
        text: visible(status) ? status.textContent : '',
        context: {visibility: document.visibilityState, width: innerWidth, height: innerHeight,
          device_pixel_ratio: devicePixelRatio}};
    })()""".replace("EXPECTED", json.dumps(expected_text))


def independent_ui_receipt(browser, url, expected_text):
    """One read outside the timed region; no concurrent observer load."""
    started = time.monotonic()
    try:
        with run.DevTools(run.page_socket(browser, url)) as page:
            response = page.call("Runtime.evaluate", {"expression": visible_expression(expected_text),
                                                       "returnByValue": True})
        if "exceptionDetails" in response:
            raise RuntimeError(str(response["exceptionDetails"]))
        return {"sample_started_at": started, "sample_finished_at": time.monotonic(),
                **response["result"]["value"]}
    except Exception as error:
        return {"sample_started_at": started, "sample_finished_at": time.monotonic(),
                "error": f"{type(error).__name__}: {error}"}


def run_case(browser, backend, fixture, *, timeout_ms=6000, poll_ms=100):
    url = owned_fixture_url(fixture)
    backend.navigate(url)
    setup = preflight(backend)
    expected_text = f"Saved record {fixture.record_id}"
    before = independent_ui_receipt(browser, url, expected_text)
    stages = []
    if fixture.case == "delayed_modal":
        stages.append(act_and_confirm(backend, "Save record", "Review before saving",
                                      timeout_ms=timeout_ms, poll_ms=poll_ms))
        # A missing review never permits a speculative confirmation click.
        if stages[-1]["confirmed"]:
            stages.append(act_and_confirm(backend, "Confirm save", expected_text,
                                          timeout_ms=timeout_ms, poll_ms=poll_ms))
    elif fixture.case == "delayed_save":
        stages.append(act_and_confirm(backend, "Save record", expected_text,
                                      timeout_ms=timeout_ms, poll_ms=poll_ms))
    else:
        raise ValueError(f"No comparable deterministic Arc policy implemented for {fixture.case}")
    after = independent_ui_receipt(browser, url, expected_text)
    evidence = fixture.evidence()
    persisted_checks = fixture.judge(evidence)
    expected_stages = 2 if fixture.case == "delayed_modal" else 1
    observed_confirmation = bool(stages) and len(stages) == expected_stages and all(stage["confirmed"] for stage in stages)
    first_started, final_returned = stages[0]["started_at"], stages[-1]["returned_at"]
    all_counts = {key: sum(stage["call_counts"][key] for stage in stages)
                  for key in stages[0]["call_counts"]}
    checks = {**persisted_checks, "expected_ui_confirmed": observed_confirmation,
              "independent_ui_receipts_healthy": not before.get("error") and not after.get("error"),
              "independent_ui_confirmation_visible": after.get("visible") is True,
              "initial_condition_absent": before.get("visible") is False,
              "exact_mutation_count": all_counts["mutations"] == expected_stages,
              "every_stage_within_deadline": all(stage["deadline_overrun_ms"] == 0 for stage in stages)}
    return {"driver": "arc-cua", "case": fixture.case, "seed": fixture.seed, "delay_ms": fixture.delay_ms,
            "matched": observed_confirmation, "expected_match": True,
            "verified_task_ms": round((final_returned - first_started) * 1000, 3),
            "observe_act_confirm_ms": round((final_returned - first_started) * 1000 +
                stages[0]["observe_act_confirm_ms"] - stages[0]["return_ms"], 3),
            "public_calls": all_counts["total_backend_calls"] - 1,
            "call_counts": all_counts, "public_calls_exclude_initial_target_observation": True,
            "checks": checks, "preflight": setup, "stages": stages,
            "independent_ui_before": before, "independent_ui_after": after,
            "persisted_evidence": evidence}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run", action="store_true", help="launch the isolated browser and run the shared fixtures")
    parser.add_argument("--case", choices=("delayed_save", "delayed_modal"), action="append", dest="cases")
    parser.add_argument("--seed", type=int, action="append", dest="seeds", help="repeat for paired fixture seeds; default 402, 403")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--headless", action="store_true", help="diagnostic only; cannot compare with visible cohorts")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cases, seeds = args.cases or ["delayed_save", "delayed_modal"], args.seeds or [402, 403]
    if args.repeats < 1 or any(seed < 0 for seed in seeds):
        parser.error("Repeats must be positive and seeds nonnegative")
    source = loaded_arc_identity()
    browser_identity = run.load_start_browser().browser_identity()
    fixture_hashes = fixture_identity()
    if not args.run:
        print(json.dumps({"status": "ready; --run is required to launch a browser", "source": source,
                          "browser": browser_identity, "fixture_and_harness_sha256": fixture_hashes,
                          "cases": cases, "seeds": seeds, "repeats": args.repeats}, indent=2))
        return 0
    from browser_expect import ExpectFixture, VERSION as FIXTURE_VERSION
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output = args.output or ROOT / "e2e/artifacts" / f"browser-expect-arc-{timestamp}"
    output.mkdir(parents=True, exist_ok=False)
    manifest = {
        "version": VERSION, "fixture_version": FIXTURE_VERSION, "driver": "arc-cua",
        "source": source, "fixture_and_harness_sha256": fixture_hashes,
        "cases": cases, "seeds": seeds, "repeats": args.repeats,
        "confirmation_policy": {"per_stage_timeout_ms": 6000, "poll_ms": 100,
            "mutations": "exactly one guarded click per workflow stage; never retry",
            "confirmation": "unmodified Arc observe snapshots until shared expected visible text"},
        "timing": {"verified_task_ms": "first action dispatch through final UI confirmation, including every poll and later stage's target observation",
            "public_calls": "Arc observe/execute calls in timed region; includes confirmation reads; excludes first target observation",
            "excluded": ["owned browser launch", "navigation", "public Revision preflight", "initial target observation", "independent before/after UI receipts", "independent judge collection"],
            "additional": "observe_act_confirm_ms includes the first target observation"},
        "not_measured": {"stale_form": "reload-and-retry workflow is outside this small comparator policy",
                         "tab_decoy": "owned second-tab decoy setup is outside this small comparator policy",
                         "focused_expect_cases": "Arc has no identical expect metadata contract; no invented API assertions"},
        "limitations": ["Deterministic policy, no model reasoning benchmark", "Arc direct Python API versus Second Cursor MCP/extension transport",
            "Explicit task-level polling is adapter behavior, not a new Arc API or upstream runtime change",
            "Native input and coexistence are not evaluated here", "DOM/CSS visibility is not compositor presentation proof",
            "An in-flight upstream observation may exceed the requested deadline and remains a measured failure",
            "Reference deadline starts at dispatch; SC baseline poll starts after click and candidate expect after input, so timeout success rates are not comparable"],
    }
    rows, browser, backend = [], None, None
    manifest_path = output / "manifest.json"
    def save():
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        (output / "browser-expect.json").write_text(json.dumps({"version": VERSION,
            "fixture_version": FIXTURE_VERSION, "label": "arc-reference", "rows": rows}, indent=2) + "\n")
    try:
        browser = arc_reference.OwnedBrowser(headed=not args.headless)
        manifest["browser"] = browser.metadata
        backend = arc_reference.attach_owned_browser(browser)
        save()
        for repeat in range(1, args.repeats + 1):
            for seed in seeds:
                for case in cases:
                    trial = output / f"{case}-{seed}-{repeat}"
                    try:
                        with ExpectFixture(case, seed, trial) as fixture:
                            row = run_case(browser, backend, fixture)
                    except Exception as error:
                        row = {"driver": "arc-cua", "case": case, "seed": seed,
                               "error": f"{type(error).__name__}: {error}"}
                    row["repeat"] = repeat
                    rows.append(row)
                    save()
                    print(json.dumps({key: value for key, value in row.items()
                        if key not in {"independent_ui_before", "independent_ui_after", "stages", "persisted_evidence"}}), flush=True)
    finally:
        try:
            if backend is not None:
                backend.close()
        finally:
            if browser is not None:
                browser.stop(output / "browser.log")
        manifest["post_run_source"] = loaded_arc_identity()
        manifest["post_run_fixture_and_harness_sha256"] = fixture_identity()
        manifest["sources_unchanged"] = (manifest["source"] == manifest["post_run_source"] and
            manifest["fixture_and_harness_sha256"] == manifest["post_run_fixture_and_harness_sha256"])
        manifest["post_run_browser_identity"] = run.load_start_browser().browser_identity()
        manifest["browser_unchanged"] = (browser_identity == manifest["post_run_browser_identity"]
                                         and (browser is None or browser.identity == browser_identity))
        save()
    print(json.dumps({"artifacts": str(output), "trials": len(rows),
                      "passed": sum(bool(row.get("checks")) and all(row["checks"].values()) for row in rows)}))
    return int(not manifest["sources_unchanged"] or not manifest["browser_unchanged"] or not rows or any(
        row.get("error") or not row.get("checks") or not all(row["checks"].values()) for row in rows))


if __name__ == "__main__":
    raise SystemExit(main())
