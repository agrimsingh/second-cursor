#!/usr/bin/env python3
"""Portable packaging and setup acceptance against a disposable Second Cursor instance.

Failure criteria live in docs/acceptance-release.md. No code signing, public publishing,
real client configuration edits, or user-tab actions occur in this suite. --browser uses
run.py's isolated browser; self-test creates and closes its own disposable test tab.
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request

import run
from run import SCENARIOS, Skip, scenario
from setup import base_env, cli, cli_binary, instance, real_home_state, check_real_home_untouched

SCENARIOS.clear()
PROJECT = pathlib.Path(__file__).resolve().parent.parent


@scenario
def release_package_integrity(ctx):
    app = pathlib.Path(instance(ctx).app)
    extension = app / "Contents/Resources/extension"
    manifest = extension / "manifest.json"
    ctx.require("packaged app contains the extension manifest", manifest.is_file(), manifest)
    data = json.loads(manifest.read_text())
    ctx.check("packaged extension requests native messaging", "nativeMessaging" in data.get("permissions", []), data.get("permissions"))
    ctx.check("packaged app contains an executable native browser helper",
              os.access(app / "Contents/MacOS/sc-native-host", os.X_OK))
    ctx.check("extension includes its actual background script", (extension / data["background"]["service_worker"]).is_file())
    result = subprocess.run(["codesign", "--verify", "--strict", "--deep", str(app)], capture_output=True, text=True)
    ctx.check("packaged app and nested helper have intact signatures", result.returncode == 0, result.stderr)


@scenario
def release_preflight_read_only(ctx):
    with tempfile.TemporaryDirectory(prefix="sc-release-check-") as folder:
        output = pathlib.Path(folder) / "not-created"
        script = PROJECT / "scripts/package-release.sh"
        proc = subprocess.run([str(script), "--check", "--output", str(output)],
                              env=base_env(SC_SIGNING_IDENTITY="Missing Second Cursor E2E Identity"),
                              capture_output=True, text=True, timeout=30)
        ctx.check("missing explicitly requested signing identity fails preflight", proc.returncode != 0,
                  proc.stdout + proc.stderr)
        ctx.check("preflight creates no output directory", not output.exists())
        ctx.check("preflight explains the missing identity", "identity" in (proc.stdout + proc.stderr).lower(), proc.stderr)


@scenario
def browser_install_portable(ctx):
    inst = instance(ctx)
    before = real_home_state()
    with tempfile.TemporaryDirectory(prefix="sc-browser-install-") as folder:
        home = pathlib.Path(folder)
        profile = home / "Library/Application Support/Google/Chrome"
        profile.mkdir(parents=True)
        unrelated = profile / "NativeMessagingHosts/another.host.json"
        unrelated.parent.mkdir()
        unrelated.write_text('{"unrelated":"kept"}')
        target = unrelated.with_name("dev.secondcursor.host.json")
        target.write_text('{"path":"/old/SecondCursor"}')
        env = {"HOME": home, "SC_HOME": inst.home}
        proc = cli(ctx, "install-browser", env=env)
        ctx.require("packaged browser installer succeeds without a checkout dependency", proc.returncode == 0,
                    proc.stdout + proc.stderr)
        installed = home / "Library/Application Support/SecondCursor/extension/manifest.json"
        ctx.check("browser extension is installed at a stable user path", installed.exists())
        value = json.loads(target.read_text())
        ctx.check("browser host points to the packaged helper",
                  value.get("path") == str(cli_binary().parent / "sc-native-host"), value)
        ctx.check("only the known extension can connect to native host",
                  value.get("allowed_origins") == ["chrome-extension://nfjjbnelcophiemlajgbmahaejppblog/"], value)
        ctx.check("stale bridge registration was backed up",
                  target.with_suffix(".json.bak").read_text() == '{"path":"/old/SecondCursor"}')
        original = target.read_bytes()
        again = cli(ctx, "install-browser", env=env)
        ctx.check("repeated registration remains idempotent", again.returncode == 0 and target.read_bytes() == original)
        ctx.check("unrelated browser registration is untouched", unrelated.read_text() == '{"unrelated":"kept"}')
        ctx.check("installer explicitly describes manual extension loading",
                  "Load unpacked" in proc.stdout and "Developer mode" in proc.stdout, proc.stdout)
        check_real_home_untouched(ctx, before, [str(home), inst.token])


@scenario
def doctor_setup_contract(ctx):
    inst = instance(ctx)
    with tempfile.TemporaryDirectory(prefix="sc-doctor-home-") as folder:
        home = pathlib.Path(folder)
        env = {"HOME": home, "SC_HOME": inst.home}
        proc = cli(ctx, "doctor", "--json", env=env)
        report = json.loads(proc.stdout)
        ctx.check("doctor authenticates and preserves its MCP transport session", report.get("reachable") is True, report)
        clients = report.get("clients", [])
        ctx.check("doctor reports all supported clients", {x["client"] for x in clients} == {"cursor", "claude", "codex", "gemini"}, clients)
        ctx.check("fresh-home clients are not presented as configured", all(not x["configured"] for x in clients), clients)
        ctx.check("doctor exposes browser repair notes", isinstance(report.get("browser_setup_notes"), list), report)
        ctx.check("doctor never prints the bearer token", inst.token not in proc.stdout + proc.stderr)
        configured = cli(ctx, "install-mcp", "--client", "codex", env=env)
        ctx.require("test client config was installed", configured.returncode == 0, configured.stderr)
        report = json.loads(cli(ctx, "doctor", "--json", env=env).stdout)
        ctx.check("doctor recognizes current client configuration", next(x for x in report["clients"] if x["client"] == "codex")["configured"])
        codex = home / ".codex/config.toml"
        current = codex.read_text()
        for label, invalid in [
            ("commented command", current.replace("command =", "# command =")),
            ("disabled server", current + "enabled = false\n"),
            ("wrong home hidden by comment", current.replace(str(inst.home), "/wrong/home") + f"# {inst.home}\n"),
        ]:
            codex.write_text(invalid)
            diagnosis = json.loads(cli(ctx, "doctor", "--json", env=env).stdout)
            ctx.check(f"doctor rejects {label}", not next(x for x in diagnosis["clients"] if x["client"] == "codex")["configured"])
        codex.write_text(current)
        check = cli(ctx, "self-test", "--json", env=env)
        result = json.loads(check.stdout)
        state = ctx.call("status")
        ready = all(state.get("permissions", {}).get(k) for k in ("accessibility", "screen_recording")) and not state.get("paused")
        ctx.check("connection check matches actual running permission state", result.get("ok") == ready, result)
        if ready:
            ctx.check("read-only connection check does not claim input verification",
                      any("not exercised" in detail for detail in result.get("details", [])), result)
        ctx.check("connection check does not leak bearer tokens", inst.token not in check.stdout + check.stderr)


@scenario
def browser_control_self_test(ctx):
    inst = instance(ctx)
    if not ctx.call("browser_status").get("connected"):
        raise Skip("requires the isolated browser extension; run with --browser")
    before = {tab["id"] for tab in ctx.call("browser_command", operation="user_tabs").get("tabs", [])}
    result = cli(ctx, "self-test", "--browser", "--json", env={"SC_HOME": inst.home}, timeout=75)
    report = json.loads(result.stdout)
    ctx.check("disposable browser control round trip succeeds", result.returncode == 0 and report.get("ok") is True, report)
    ctx.check("success records an independently observed result", any("independently verified" in item for item in report.get("details", [])), report)
    after = {tab["id"] for tab in ctx.call("browser_command", operation="user_tabs").get("tabs", [])}
    ctx.check("test closes its owned disposable tab and preserves existing tabs", before == after, {"before": sorted(before), "after": sorted(after)})
    history = json.loads((inst.home / "tasks.json").read_text())["tasks"]
    checks = [task for task in history if task.get("title") == "Browser setup check"]
    ctx.check("successful check leaves a named completed task", checks and all(task["status"] == "completed" for task in checks), checks)


def browser_inventory(browser):
    port = (browser.profile / "DevToolsActivePort").read_text().splitlines()[0]
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
        return {page["id"] for page in json.load(response) if page.get("type") == "page"}


@scenario
def browser_control_failure_cleanup(ctx):
    # Deny exactly the click tool after browser_open succeeds. Inventory through the isolated
    # browser's DevTools port so this is independent of MCP session visibility and approvals.
    with run.TestInstance({"approvals": "deny", "appApproval": "off", "confirmationRequiredTools": ["browser_command"]},
                          app=instance(ctx).app, browser=True) as isolated:
        before = browser_inventory(isolated.browser)
        result = cli(ctx, "self-test", "--browser", "--json", env={"SC_HOME": isolated.home}, timeout=75)
        report = json.loads(result.stdout)
        ctx.check("a refused test click is reported as failure", result.returncode != 0 and report.get("ok") is False, report)
        ctx.check("failed browser test removes its disposable tab", browser_inventory(isolated.browser) == before)
        history = json.loads((isolated.home / "tasks.json").read_text())["tasks"]
        checks = [task for task in history if task.get("title") == "Browser setup check"]
        ctx.check("failed check leaves a named failed task", checks and all(task["status"] == "failed" for task in checks), checks)


if __name__ == "__main__":
    sys.exit(0 if run.Run(config_overrides={"approvals": "allow", "appApproval": "off"}).run(set(sys.argv[1:])) else 1)
