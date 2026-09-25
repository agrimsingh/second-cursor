#!/usr/bin/env python3
"""Test-instance isolation contract: SC_HOME relocates everything the app writes, the browser bridge
follows it, and the harness starts and stops instances without touching the user's app or files.

Usage: e2e/instance.py [scenario ...]    (SC_E2E_APP=build/SecondCursor.app to test a fresh build)
"""

import json, os, pathlib, stat, subprocess, sys, time, uuid

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import run as harness
from run import Run, SCENARIOS, scenario, Skip, TestInstance, alive, tail

SCENARIOS.clear()

USER_SUPPORT = pathlib.Path.home() / "Library/Application Support/SecondCursor"
USER_ACTION_LOG = pathlib.Path.home() / "Library/Logs/SecondCursor/actions.jsonl"


def instance(ctx):
    if ctx.run.instance is None:
        raise Skip("--installed targets the user's app; there is no test instance to check")
    return ctx.run.instance


def app_processes():
    """(pid, command-with-environment) for every running Second Cursor app process."""
    listing = subprocess.run(["ps", "-Eww", "-axo", "pid=,command="], capture_output=True, text=True).stdout
    return [(int(line.split(None, 1)[0]), line) for line in listing.splitlines() if "Contents/MacOS/SecondCursor" in line]


@scenario
def home_layout(ctx):
    app = instance(ctx)
    ctx.check("SC_HOME keeps the browser socket path under 104 bytes", len(str(app.home / "browser.sock")) < 104, app.home)
    for name in ["config.json", "instance.lock", "instance.pid"]:
        ctx.check(f"{name} is in SC_HOME", (app.home / name).exists(), sorted(p.name for p in app.home.iterdir()))
    pid = int((app.home / "instance.pid").read_text())
    command = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    ctx.check("instance.pid names a running process of the bundle under test", str(app.binary) in command, command)
    sock = app.home / "browser.sock"
    ctx.check("the browser socket is in SC_HOME", sock.exists() and stat.S_ISSOCK(sock.stat().st_mode), sock)
    ctx.check("harness CONFIG_PATH points at the test instance", harness.CONFIG_PATH == app.config_path, harness.CONFIG_PATH)
    config = json.loads(app.config_path.read_text())
    ctx.check("test config turns per-app approval off", config.get("appApproval") == "off", config.get("appApproval"))
    if "--approvals" not in sys.argv:
        ctx.check('test config grants approvals without UI ("allow")', config.get("approvals") == "allow",
                  config.get("approvals"))
        try:
            TestInstance({"approvals": "prompt"}, app=app.app)
        except RuntimeError as e:
            ctx.check('an "approvals": "prompt" instance is refused without --approvals', "--approvals" in str(e), e)
        else:
            ctx.check('an "approvals": "prompt" instance is refused without --approvals', False, "it started")


@scenario
def action_log_isolated(ctx):
    app = instance(ctx)
    user_log_size = USER_ACTION_LOG.stat().st_size if USER_ACTION_LOG.exists() else 0
    marker = f"isolation-{uuid.uuid4().hex[:8]}.txt"
    window = ctx.launch_scratch_textedit(marker, "isolated")
    ctx.call("press_key", window_id=window["id"], keys="right")
    text = app.action_log.read_text() if app.action_log.exists() else ""
    ctx.check("actions are logged to $SC_HOME/logs/actions.jsonl", marker in text and '"press_key"' in text, text[-300:])
    ctx.check("harness ACTION_LOG points at the test instance", harness.ACTION_LOG == app.action_log, harness.ACTION_LOG)
    appended = b""
    if USER_ACTION_LOG.exists():
        with open(USER_ACTION_LOG, "rb") as log:
            log.seek(user_log_size)
            appended = log.read()
    ctx.check("nothing from this run reached the user's action log", marker.encode() not in appended)


@scenario
def pause_isolated(ctx):
    app = instance(ctx)
    user_flag = USER_SUPPORT / "paused"
    user_paused = user_flag.exists()
    # No explicit env: scenarios rely on the harness exporting SC_HOME to every subprocess.
    subprocess.run([str(harness.APP_BINARY), "--pause"], check=True, capture_output=True, timeout=10)
    try:
        ctx.check("APP_BINARY --pause writes $SC_HOME/paused", (app.home / "paused").exists())
        ctx.check("the test instance reports paused", ctx.call("status")["paused"] is True)
        ctx.check("the user's pause flag is untouched", user_flag.exists() == user_paused)
    finally:
        subprocess.run([str(harness.APP_BINARY), "--resume"], check=True, capture_output=True, timeout=10)
    ctx.check("--resume clears it", not (app.home / "paused").exists() and ctx.call("status")["paused"] is False)
    ctx.check("the user's pause flag is still untouched", user_flag.exists() == user_paused)


@scenario
def native_host_follows_sc_home(ctx):
    app = instance(ctx)
    host = app.app / "Contents/MacOS/sc-native-host"
    if not os.access(host, os.X_OK):
        raise Skip(f"{host} missing")
    relay = subprocess.Popen([str(host), "chrome-extension://e2e/"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=app.env)
    time.sleep(1)
    connected = relay.poll() is None
    relay.stdin.close()
    try:
        relay.wait(5)
    except subprocess.TimeoutExpired:
        relay.kill()
    ctx.check("sc-native-host with SC_HOME connects to the test instance's socket", connected, relay.stderr.read()[-300:])
    empty = {**os.environ, "SC_HOME": str(ctx.scratch)}
    missing = subprocess.run([str(host)], input=b"", capture_output=True, env=empty, timeout=5)
    ctx.check("with an SC_HOME that has no app it reports not running instead of reaching the user's app",
              missing.returncode != 0 and b"not running" in missing.stdout, (missing.returncode, missing.stdout[-200:]))


@scenario
def second_instance_lifecycle(ctx):
    main = instance(ctx)
    untagged_before = {pid for pid, line in app_processes() if "SC_HOME=" not in line}
    other = TestInstance({"claimIdleSeconds": 7}, app=main.app)
    ctx.cleanups.append(other.stop)
    home, pid = other.home, other.pid
    ctx.check("a second test instance runs alongside the first", pid and pid != main.pid and alive(pid), (pid, main.pid))
    ctx.check("it has its own home, port and token",
              home != main.home and other.port != main.port and other.token != main.token)
    ctx.check("config overrides land in its config", json.loads(other.config_path.read_text()).get("claimIdleSeconds") == 7)
    ctx.check("it answers MCP with its own token", "paused" in other.mcp.call("status")[0])
    probe = {"jsonrpc": "2.0", "id": 0, "method": "tools/list", "params": {}}
    status, _, _ = harness.MCP(other.url, main.token).raw(probe)
    ctx.check("it rejects the other instance's token", status == 401, status)
    other.stop()
    ctx.check("stop() ends its process", not alive(pid), pid)
    ctx.check("stop() deletes its SC_HOME", not home.exists(), home)
    ctx.check("the first test instance is still serving", "paused" in ctx.call("status"))
    untagged_after = {pid for pid, line in app_processes() if "SC_HOME=" not in line}
    ctx.check("app processes without SC_HOME (the user's app) were left alone", untagged_before <= untagged_after,
              (untagged_before, untagged_after))


@scenario
def isolated_browser(ctx):
    app = instance(ctx)
    browsers = ctx.call("browser_instances")["browsers"]
    ctx.require("the isolated test browser is connected to the test instance", browsers,
                tail(app.browser.log) if app.browser else "no test browser was started")
    listing = subprocess.run(["ps", "-Eww", "-axo", "command="], capture_output=True, text=True).stdout
    hosts = [line for line in listing.splitlines()
             if "sc-native-host" in line and f" SC_HOME={app.home} " in line + " "]
    ctx.check("Chrome launched sc-native-host with this SC_HOME", hosts, len(hosts))


if __name__ == "__main__":
    run = Run()
    sys.exit(0 if run.run(sys.argv[1:]) else 1)
