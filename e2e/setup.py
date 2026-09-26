#!/usr/bin/env python3
"""E2E for setup and distribution: the stdio `mcp` proxy, `doctor`, `--turn-ended`, `install-mcp`, and
onboarding gating.

Every CLI invocation is a real subprocess against the disposable test instance (SC_HOME) and, for
install-mcp, a temporary HOME. The user's real client configs are only read, to prove nothing touched them.

Usage:  python3 e2e/setup.py [scenario ...]
        SC_E2E_CLI=.build/debug/SecondCursor python3 e2e/setup.py    # exercise an unbundled CLI build
Output: e2e/artifacts/<run-id>/ (report, transcript, and cli/<scenario>-<n>.txt for every CLI invocation)
"""
import json
import os
import pathlib
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid

import run
from run import SCENARIOS, Skip, ToolError, free_port, observe, scenario

SCENARIOS.clear()

REAL_HOME = pathlib.Path(os.path.expanduser("~"))
CLIENT_FILES = {
    "cursor": ".cursor/mcp.json",
    "claude": ".claude.json",
    "gemini": ".gemini/settings.json",
    "codex": ".codex/config.toml",
}
HOOKS_FILE = ".cursor/hooks.json"
# Every instance these scenarios start must never show an approval dialog.
NO_UI = {"approvals": "allow", "appApproval": "off"}
INITIALIZE = {
    "protocolVersion": run.PROTOCOL_VERSION,
    "capabilities": {},
    "clientInfo": {"name": "second-cursor-setup-e2e", "version": "1"},
}


def cli_binary():
    override = os.environ.get("SC_E2E_CLI")
    return pathlib.Path(override).resolve() if override else pathlib.Path(run.APP_BINARY)


def instance(ctx):
    inst = getattr(ctx.run, "instance", None)
    if inst is None:
        raise Skip("needs a disposable test instance (not the installed app)")
    return inst


def same_path(a, b):
    return os.path.realpath(str(a)) == os.path.realpath(str(b))


def base_env(**overrides):
    env = {k: v for k, v in os.environ.items() if k != "SC_HOME"}
    env.update({k: str(v) for k, v in overrides.items()})
    return env


def record(ctx, argv, env, code, stdout, stderr, seconds, stdin=""):
    folder = ctx.run.dir / "cli"
    folder.mkdir(exist_ok=True)
    n = len(list(folder.glob(f"{ctx.name}-*.txt")))
    lines = [f"$ {shlex.join(argv)}", f"env: {json.dumps({k: str(v) for k, v in env.items()})}",
             f"exit: {code}  ({seconds:.2f}s)"]
    if stdin:
        lines += ["--- stdin", stdin.rstrip("\n")]
    lines += ["--- stdout", stdout.rstrip("\n"), "--- stderr", stderr.rstrip("\n")]
    (folder / f"{ctx.name}-{n}.txt").write_text("\n".join(lines) + "\n")


def cli(ctx, *args, env=None, stdin="", timeout=30):
    """Runs the CLI with stdin closed after `stdin` and records the invocation."""
    env = env or {}
    argv = [str(cli_binary()), *args]
    started = time.time()
    proc = subprocess.run(argv, input=stdin, env=base_env(**env), capture_output=True, text=True, timeout=timeout)
    record(ctx, argv, env, proc.returncode, proc.stdout, proc.stderr, time.time() - started, stdin)
    return proc


def rpc(msg_id, method, params=None):
    message = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if msg_id is not None:
        message["id"] = msg_id
    return message


def stdio(ctx, messages, env, command=None, timeout=45):
    """Writes every message to `SecondCursor mcp` at once, closes stdin, and waits for exit.
    Returns (exit code, stdout lines parsed as JSON, raw stdout, stderr)."""
    argv = command or [str(cli_binary()), "mcp"]
    stdin = "".join((m if isinstance(m, str) else json.dumps(m)) + "\n" for m in messages)
    started = time.time()
    proc = subprocess.run(argv, input=stdin, env=base_env(**env), capture_output=True, text=True, timeout=timeout)
    record(ctx, argv, env, proc.returncode, proc.stdout, proc.stderr, time.time() - started, stdin)
    parsed = []
    for line in proc.stdout.splitlines():
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            parsed.append({"_unparsed": line})
    return proc.returncode, parsed, proc.stdout, proc.stderr


def parse_json(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def tool_json(response):
    result = response.get("result", {})
    text = next((c["text"] for c in result.get("content", []) if c.get("type") == "text"), None)
    return None if result.get("isError") or text is None else parse_json(text)


def instance_pid(home):
    try:
        return int((pathlib.Path(home) / "instance.pid").read_text().strip())
    except (OSError, ValueError):
        return None


def launch(app, home, *args, timeout=20):
    """Launches `app` for an SC_HOME the way TestInstance does, plus launch arguments. Returns its pid."""
    subprocess.run(["open", "-n", "-g", "--env", f"SC_HOME={home}", "-a", str(app), *(["--args", *args] if args else [])],
                   check=True, capture_output=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pid := instance_pid(home):
            return pid
        time.sleep(0.25)
    raise AssertionError(f"{app} wrote no instance.pid in {home} within {timeout}s")


class TempHome:
    """A short scratch SC_HOME (socket paths cap at 104 bytes). Stops any instance that ends up running in it."""

    def __init__(self, port=None, token=None, config=True):
        self.port, self.token, self.config = port, token or uuid.uuid4().hex, config

    def __enter__(self):
        self.path = pathlib.Path(tempfile.mkdtemp(prefix="sc-", dir="/tmp"))
        if self.config:
            (self.path / "config.json").write_text(json.dumps({
                "port": self.port or free_port(), "token": self.token,
                "blockedBundleIDs": run.DEFAULT_BLOCKED_BUNDLE_IDS, "confirmationRequiredTools": [],
                **NO_UI,
            }))
        return self

    def __exit__(self, *exc):
        if pid := instance_pid(self.path):
            run.terminate(pid)
        shutil.rmtree(self.path, ignore_errors=True)


class HangUpListener:
    """A localhost port that accepts connections and closes them without answering."""

    def __enter__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)

        def loop():
            while True:
                try:
                    conn, _ = self.sock.accept()
                    conn.close()
                except OSError:
                    return

        threading.Thread(target=loop, daemon=True).start()
        return self.sock.getsockname()[1]

    def __exit__(self, *exc):
        self.sock.close()


@scenario
def cli_help(ctx):
    proc = cli(ctx, "--help")
    ctx.check("--help exits 0", proc.returncode == 0, proc.stderr)
    for word in ["mcp", "doctor", "install-mcp", "--turn-ended", "--show-onboarding", "--pause", "--resume",
                 "--dry-run", "--with-hook", "--session", "--json"]:
        ctx.check(f"--help documents {word}", word in proc.stdout, proc.stdout[:400])
    bad = cli(ctx, "install-mcp", "--client", "emacs", "--dry-run")
    ctx.check("install-mcp rejects an unknown client with a non-zero exit", bad.returncode != 0, bad.stdout + bad.stderr)
    bad = cli(ctx, "install-mcp")
    ctx.check("install-mcp without --client is refused", bad.returncode != 0, bad.stdout + bad.stderr)


@scenario
def mcp_stdio(ctx):
    inst = instance(ctx)
    code, out, raw, err = stdio(ctx, [
        rpc(1, "initialize", INITIALIZE),
        rpc(None, "notifications/initialized"),
        rpc("two", "tools/list"),
        "{this is not json",
        rpc(3, "tools/call", {"name": "status", "arguments": {}}),
    ], env={"SC_HOME": inst.home})
    ctx.check("exits 0 after stdin EOF, once in-flight requests are answered", code == 0, f"exit {code}; {err[-300:]}")
    ctx.check("stdout carries only JSON-RPC lines", out and all(m.get("jsonrpc") == "2.0" for m in out), raw[:500])
    by_id = {json.dumps(m.get("id")): m for m in out}
    ctx.check("one answer per request plus one parse error; nothing for the notification",
              len(out) == 4 and sorted(by_id) == sorted(["1", '"two"', "3", "null"]), [m.get("id") for m in out])
    init = by_id.get("1", {}).get("result", {})
    ctx.check("initialize reaches the app", init.get("serverInfo", {}).get("name") == "second-cursor", init)
    tools = {t["name"] for t in by_id.get('"two"', {}).get("result", {}).get("tools", [])}
    ctx.check("tools/list returns the catalog (string ids work)", {"status", "get_window_state", "click"} <= tools,
              sorted(tools)[:10])
    status = tool_json(by_id.get("3", {})) or {}
    ctx.check("tools/call status returns live runtime state", "permissions" in status, by_id.get("3"))
    ctx.check("malformed input gets a JSON-RPC parse error with id null",
              by_id.get("null", {}).get("error", {}).get("code") == -32700, by_id.get("null"))


@scenario
def mcp_stdio_concurrent(ctx):
    inst = instance(ctx)
    window = ctx.launch_scratch_textedit("proxy.txt", "slow call target")
    slow = rpc("slow", "tools/call", {"name": "batch", "arguments": {
        "window_id": window["id"], "steps": [{"tool": "wait", "args": {"ms": 3000}}], "observe": "none"}})
    fast = rpc("fast", "tools/call", {"name": "status", "arguments": {}})
    started = time.time()
    code, out, raw, err = stdio(ctx, [slow, fast], env={"SC_HOME": inst.home})
    elapsed = time.time() - started
    ids = [m.get("id") for m in out]
    ctx.check("both requests are answered", sorted(map(str, ids)) == ["fast", "slow"], ids)
    ctx.check("requests are in flight concurrently: the fast answer is not held behind the slow one",
              ids[:1] == ["fast"], ids)
    ctx.check("the slow call really ran before exit (>= 3 s)", elapsed >= 3, f"{elapsed:.1f}s")
    slow_result = next((m for m in out if m.get("id") == "slow"), {})
    ctx.check("the slow batch succeeded", tool_json(slow_result) is not None, slow_result)
    ctx.check("exit 0", code == 0, err[-300:])


@scenario
def mcp_stdio_errors(ctx):
    inst = instance(ctx)
    with TempHome(port=inst.port, token="not-the-token") as home:
        code, out, raw, err = stdio(ctx, [rpc(7, "initialize", INITIALIZE), rpc(8, "tools/list")],
                                    env={"SC_HOME": home.path})
        ctx.check("wrong token: every request still gets exactly one answer with its own id",
                  sorted(m.get("id", -1) for m in out) == [7, 8], raw[:400])
        ctx.check("wrong token: answers are JSON-RPC errors that say why",
                  out and all("error" in m and re.search(r"401|unauthori|token", json.dumps(m), re.I) for m in out), out)
        ctx.check("wrong token: exits 0 on EOF", code == 0, err[-300:])

    with HangUpListener() as port, TempHome(port=port) as home:
        started = time.time()
        code, out, raw, err = stdio(ctx, [rpc(9, "tools/list")], env={"SC_HOME": home.path})
        ctx.check("broken server: answered with a JSON-RPC error carrying the request id",
                  len(out) == 1 and out[0].get("id") == 9 and "error" in out[0], raw[:400])
        ctx.check("broken server: no launch-and-retry loop (answer within 5 s)", time.time() - started < 5,
                  f"{time.time() - started:.1f}s")
        ctx.check("broken server: nothing was launched for that home", not (home.path / "instance.pid").exists())


@scenario
def mcp_autolaunch(ctx):
    if ".app/Contents/MacOS/" not in str(cli_binary()):
        raise Skip("CLI is not inside an app bundle; auto-launch opens the enclosing bundle")
    with TempHome() as home:
        started = time.time()
        code, out, raw, err = stdio(ctx, [rpc(1, "initialize", INITIALIZE)], env={"SC_HOME": home.path})
        elapsed = time.time() - started
        ctx.check("app not running: the proxy launches it for its SC_HOME and answers initialize",
                  out and out[0].get("result", {}).get("serverInfo", {}).get("name") == "second-cursor",
                  raw[:300] or err[-300:])
        ctx.check("launch and answer within ~15 s", elapsed < 15, f"{elapsed:.1f}s")
        ctx.check("the launched instance lives in that SC_HOME", (home.path / "instance.pid").exists())


@scenario
def doctor(ctx):
    inst = instance(ctx)
    env = {"SC_HOME": inst.home}
    proc = cli(ctx, "doctor", "--json", env=env)
    report = parse_json(proc.stdout)
    ctx.require("doctor --json prints one JSON object", isinstance(report, dict), proc.stdout[:300])
    ctx.check("reports the instance as reachable at its URL",
              report.get("reachable") is True and str(inst.port) in str(report.get("url")), report)
    ctx.check("reports the running app's version", bool(report.get("version")), report)
    ctx.check("reports the instance's config path",
              same_path(report.get("config_path", ""), pathlib.Path(inst.home) / "config.json"), report)
    permissions = report.get("permissions") or {}
    ctx.check("reports permissions, pause and extension state",
              {"accessibility", "screen_recording"} <= set(permissions)
              and isinstance(report.get("paused"), bool) and isinstance(report.get("extension_connected"), bool), report)
    healthy = permissions.get("accessibility") and permissions.get("screen_recording") and not report.get("paused")
    ctx.check("exit code is 0 exactly when nothing needs action", (proc.returncode == 0) == bool(healthy),
              f"exit {proc.returncode}, problems {report.get('problems')}")

    human = cli(ctx, "doctor", env=env)
    ctx.check("human report covers every item",
              all(w in human.stdout for w in ["Accessibility", "Screen Recording", "aused", "xtension", "onfig"]),
              human.stdout)
    ctx.check("human report and --json agree on the exit code", human.returncode == proc.returncode)

    cli(ctx, "--pause", env=env)
    try:
        paused = cli(ctx, "doctor", "--json", env=env)
        rep = parse_json(paused.stdout) or {}
        ctx.check("paused: doctor exits non-zero", paused.returncode != 0, paused.stdout[:300])
        ctx.check("paused: reported with a problem that says how to resume",
                  rep.get("paused") is True and any("resume" in p.lower() for p in rep.get("problems", [])), rep)
    finally:
        cli(ctx, "--resume", env=env)

    with TempHome() as home:
        down = cli(ctx, "doctor", "--json", env={"SC_HOME": home.path})
        rep = parse_json(down.stdout) or {}
        ctx.check("app not running: exit non-zero with valid JSON saying unreachable",
                  down.returncode != 0 and rep.get("reachable") is False, down.stdout[:300])
        human = cli(ctx, "doctor", env={"SC_HOME": home.path})
        ctx.check("app not running: says so and how to start it",
                  re.search(r"not running", human.stdout, re.I) and "open " in human.stdout, human.stdout)
        ctx.check("doctor never launches the app", not (home.path / "instance.pid").exists())

    with TempHome(config=False) as home:
        missing = cli(ctx, "doctor", "--json", env={"SC_HOME": home.path})
        ctx.check("no config yet: exit non-zero with valid JSON",
                  missing.returncode != 0 and isinstance(parse_json(missing.stdout), dict), missing.stdout[:300])
        ctx.check("no config yet: doctor has no side effects", not (home.path / "config.json").exists())


@scenario
def turn_ended(ctx):
    inst = instance(ctx)
    env = {"SC_HOME": inst.home}
    hook_input = json.dumps({"conversation_id": "setup-e2e", "generation_id": "g1", "status": "completed",
                             "hook_event_name": "stop", "workspace_roots": [str(run.ROOT.parent)]})
    proc = cli(ctx, "--turn-ended", env=env, stdin=hook_input)
    out = parse_json(proc.stdout)
    ctx.check("exit 0", proc.returncode == 0, proc.stderr)
    ctx.check("prints the documented JSON for the default (legacy) session",
              isinstance(out, dict) and out.get("ok") is True and out.get("session_id") == "legacy"
              and isinstance(out.get("released_tabs"), int) and isinstance(out.get("cleared_app_approvals"), int),
              proc.stdout[:300] or proc.stderr[-300:])

    session = ctx.call("session", action="create")["session_id"]
    ctx.cleanups.append(lambda: ctx.mcp.call("session", action="close", session_id=session))
    proc = cli(ctx, "--turn-ended", "--session", session, env=env)
    ctx.check("--session targets that session", (parse_json(proc.stdout) or {}).get("session_id") == session,
              proc.stdout[:300] or proc.stderr[-300:])

    argv = [str(cli_binary()), "--turn-ended"]
    started = time.time()
    held = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env=base_env(**env), text=True)
    held.stdin.write(hook_input)
    held.stdin.flush()
    try:
        code = held.wait(timeout=15)
    except subprocess.TimeoutExpired:
        held.kill()
        code = None
    elapsed = time.time() - started
    stdout, stderr = held.stdout.read(), held.stderr.read()
    held.stdin.close()
    record(ctx, argv, env, code, stdout, stderr, elapsed, hook_input + "\n(stdin left open)")
    ctx.check("does not hang when the hook host keeps stdin open", code == 0 and elapsed < 15, f"exit {code}, {elapsed:.1f}s")

    with TempHome() as home:
        started = time.time()
        down = cli(ctx, "--turn-ended", env={"SC_HOME": home.path}, stdin=hook_input)
        ctx.check("app not running: still exits 0 (hooks must never fail loudly)", down.returncode == 0)
        ctx.check("app not running: explains on stderr and prints nothing on stdout",
                  down.stdout.strip() == "" and down.stderr.strip() != "", down.stdout + down.stderr)
        ctx.check("app not running: fast and launches nothing",
                  time.time() - started < 10 and not (home.path / "instance.pid").exists())


# install-mcp fixtures: unrelated entries that must survive a merge, plus stale second-cursor entries to replace.
# Every other server carries a fake secret that install-mcp must never print.
PLANTED_SECRET = f"sk-e2e-planted-{uuid.uuid4().hex}"
UNRELATED = {
    "cursor": {"mcpServers": {"other": {"url": "https://other.example/mcp",
                                        "headers": {"Authorization": f"Bearer {PLANTED_SECRET}"}}},
               "note": "keep me"},
    "claude": {"numStartups": 5, "projects": {"/somewhere": {"allowedTools": ["Bash"]}},
               "mcpServers": {"other": {"type": "stdio", "command": "other-server", "env": {"OTHER_API_KEY": PLANTED_SECRET}},
                              "second-cursor": {"type": "stdio", "command": "/old/SecondCursor", "args": ["mcp"]}}},
    "gemini": {"theme": "Dark", "mcpServers": {"other": {"command": "other-server", "env": {"OTHER_API_KEY": PLANTED_SECRET}}}},
}
CODEX_OTHER = {"command": "other-server", "args": ["--flag"], "env": {"OTHER_API_KEY": PLANTED_SECRET}}
CODEX_TOML = f"""model = "o3"
approval_policy = "never"

[mcp_servers.other]
command = "other-server"
args = ["--flag"]
env = {{ OTHER_API_KEY = "{PLANTED_SECRET}" }}

[mcp_servers.second_cursor]
command = "/old/SecondCursor"
args = ["mcp"]

[profiles.fast]
model = "gpt-5"
"""
# What a dry run must name as kept, per client.
KEPT_NAMES = {
    "cursor": ["mcpServers.other", "note"],
    "claude": ["mcpServers.other", "numStartups", "projects"],
    "gemini": ["mcpServers.other", "theme"],
    "codex": ["model", "approval_policy", "mcp_servers.other", "profiles.fast"],
}
HOOKS = {"version": 1, "hooks": {"preToolUse": [{"command": "echo keep-me", "timeout": 5}]}}


def seed(home):
    for client, data in UNRELATED.items():
        path = home / CLIENT_FILES[client]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n")
    (home / CLIENT_FILES["claude"]).chmod(0o600)
    (home / CLIENT_FILES["codex"]).parent.mkdir(parents=True, exist_ok=True)
    (home / CLIENT_FILES["codex"]).write_text(CODEX_TOML)
    (home / HOOKS_FILE).write_text(json.dumps(HOOKS, indent=2) + "\n")


def real_home_state():
    """mtimes of the user's real client configs and their .bak files. ~/.claude.json itself is rewritten by
    Claude Code at any time, so only its .bak is compared by mtime; its content is checked for leaks instead."""
    state = {}
    for rel in [*CLIENT_FILES.values(), HOOKS_FILE]:
        for path in [REAL_HOME / rel, REAL_HOME / (rel + ".bak")]:
            if path == REAL_HOME / CLIENT_FILES["claude"]:
                continue
            state[str(path)] = path.stat().st_mtime_ns if path.exists() else None
    return state


def real_home_mentions(needles):
    hits = []
    for rel in [*CLIENT_FILES.values(), HOOKS_FILE]:
        path = REAL_HOME / rel
        if path.exists():
            text = path.read_text(errors="replace")
            hits += [f"{rel} mentions {n}" for n in needles if n in text]
    return hits


def check_real_home_untouched(ctx, before, needles):
    after = real_home_state()
    changed = [p for p in before if before[p] != after[p]]
    ctx.check("the user's real client configs and backups were not modified", not changed, changed)
    leaks = real_home_mentions(needles)
    ctx.check("the user's real client configs never reference the test instance", not leaks, leaks)


def expected_stdio_entry(inst):
    return {"command": os.path.realpath(cli_binary()), "args": ["mcp"], "env": {"SC_HOME": str(inst.home)}}


def stdio_entry_ok(entry, inst):
    want = expected_stdio_entry(inst)
    return (isinstance(entry, dict) and same_path(entry.get("command", ""), want["command"])
            and entry.get("args") == ["mcp"] and same_path((entry.get("env") or {}).get("SC_HOME", ""), inst.home))


def cursor_entry_ok(entry, inst, redacted=False):
    token = f"…{inst.token[-4:]}" if redacted else inst.token
    return (isinstance(entry, dict) and entry.get("url") == f"http://127.0.0.1:{inst.port}/mcp"
            and entry.get("headers", {}).get("Authorization") == f"Bearer {token}")


def load_document(client, text):
    return tomllib.loads(text) if client == "codex" else json.loads(text)


def entry_of(client, doc):
    if client == "codex":
        return doc.get("mcp_servers", {}).get("second_cursor")
    return doc.get("mcpServers", {}).get("second-cursor")


def unrelated_preserved(client, doc):
    if client == "codex":
        return (doc.get("model") == "o3" and doc.get("approval_policy") == "never"
                and doc.get("mcp_servers", {}).get("other") == CODEX_OTHER
                and doc.get("profiles", {}).get("fast") == {"model": "gpt-5"})
    original = UNRELATED[client]
    return (all(doc.get(k) == v for k, v in original.items() if k != "mcpServers")
            and doc.get("mcpServers", {}).get("other") == original["mcpServers"]["other"])


def dry_run_parts(stdout):
    """Dry-run output is comment lines starting with '# ' followed by only our entry, in the file's format.
    Returns (comment text, entry document text)."""
    lines = stdout.splitlines()
    comments = []
    while lines and lines[0].startswith("# "):
        comments.append(lines.pop(0))
    return "\n".join(comments), "\n".join(lines)


def only_our_entry(client, doc, entry):
    return doc == ({"mcp_servers": {"second_cursor": entry}} if client == "codex" else {"mcpServers": {"second-cursor": entry}})


def leaks(text, inst):
    return [name for name, secret in (("the bearer token", inst.token), ("another server's secret", PLANTED_SECRET))
            if secret in text]


@scenario
def install_mcp_dry_run(ctx):
    inst = instance(ctx)
    before = real_home_state()
    home = pathlib.Path(tempfile.mkdtemp(prefix="sc-home-"))
    ctx.cleanups.append(lambda: shutil.rmtree(home, ignore_errors=True))
    seed(home)
    originals = {c: (home / rel).read_bytes() for c, rel in CLIENT_FILES.items()}
    env = {"HOME": home, "SC_HOME": inst.home}
    for client, rel in CLIENT_FILES.items():
        proc = cli(ctx, "install-mcp", "--client", client, "--dry-run", env=env)
        ctx.check(f"{client}: dry run exits 0", proc.returncode == 0, proc.stderr[-300:])
        leaked = leaks(proc.stdout + proc.stderr, inst)
        ctx.check(f"{client}: dry run prints neither our token nor another server's secret", not leaked, leaked)
        comments, body = dry_run_parts(proc.stdout)
        try:
            doc = load_document(client, body)
        except (ValueError, tomllib.TOMLDecodeError) as e:
            ctx.check(f"{client}: dry run prints our entry in the file's format", False, f"{e}: {proc.stdout[:300]}")
            continue
        entry = entry_of(client, doc)
        ok = cursor_entry_ok(entry, inst, redacted=True) if client == "cursor" else stdio_entry_ok(entry, inst)
        ctx.check(f"{client}: dry run shows the second-cursor entry"
                  + (" with the token cut to its last 4 characters" if client == "cursor" else ""), ok, entry)
        ctx.check(f"{client}: dry run shows nothing but our entry", only_our_entry(client, doc, entry), doc)
        missing = [name for name in KEPT_NAMES[client] if name not in comments]
        ctx.check(f"{client}: dry run names what else the file keeps", not missing, f"missing {missing}: {comments}")
        ctx.check(f"{client}: dry run writes nothing (file unchanged, no .bak)",
                  (home / rel).read_bytes() == originals[client] and not (home / (rel + ".bak")).exists())

    hooks_original = (home / HOOKS_FILE).read_bytes()
    proc = cli(ctx, "install-mcp", "--client", "cursor", "--with-hook", "--dry-run", env=env)
    leaked = leaks(proc.stdout + proc.stderr, inst)
    ctx.check("--with-hook dry run: exit 0, no token or other secret printed", proc.returncode == 0 and not leaked,
              f"exit {proc.returncode}, leaked {leaked}")
    ctx.check("--with-hook dry run: hooks.json unchanged and no .bak",
              (home / HOOKS_FILE).read_bytes() == hooks_original and not (home / (HOOKS_FILE + ".bak")).exists())
    check_real_home_untouched(ctx, before, [str(home), inst.token])


@scenario
def install_mcp_write(ctx):
    inst = instance(ctx)
    before = real_home_state()
    home = pathlib.Path(tempfile.mkdtemp(prefix="sc-home-"))
    ctx.cleanups.append(lambda: shutil.rmtree(home, ignore_errors=True))
    seed(home)
    originals = {c: (home / rel).read_bytes() for c, rel in CLIENT_FILES.items()}
    env = {"HOME": home, "SC_HOME": inst.home}

    for client, rel in CLIENT_FILES.items():
        path = home / rel
        proc = cli(ctx, "install-mcp", "--client", client, env=env)
        ctx.check(f"{client}: install exits 0", proc.returncode == 0, proc.stderr[-300:])
        ctx.check(f"{client}: output says what changed and where", str(path) in proc.stdout or rel in proc.stdout,
                  proc.stdout[:300])
        leaked = leaks(proc.stdout + proc.stderr, inst)
        ctx.check(f"{client}: a real write echoes no secrets", not leaked, leaked)
        try:
            doc = load_document(client, path.read_text())
        except (ValueError, tomllib.TOMLDecodeError) as e:
            ctx.check(f"{client}: written file parses", False, e)
            continue
        entry = entry_of(client, doc)
        ok = cursor_entry_ok(entry, inst) if client == "cursor" else stdio_entry_ok(entry, inst)
        ctx.check(f"{client}: file now carries the second-cursor entry", ok, entry)
        ctx.check(f"{client}: every unrelated entry survived the merge", unrelated_preserved(client, doc), doc)
        backup = home / (rel + ".bak")
        ctx.check(f"{client}: .bak holds the exact original", backup.exists() and backup.read_bytes() == originals[client])
        if client == "codex":
            text = path.read_text()
            ctx.check("codex: the stale table was replaced, not duplicated",
                      text.count("[mcp_servers.second_cursor]") == 1 and "/old/SecondCursor" not in text, text)
        written = path.read_bytes()
        again = cli(ctx, "install-mcp", "--client", client, env=env)
        ctx.check(f"{client}: re-running is idempotent (exit 0, file and .bak unchanged)",
                  again.returncode == 0 and path.read_bytes() == written and backup.read_bytes() == originals[client],
                  again.stdout[:300])
    ctx.check("claude: a private (0600) config stays private",
              (home / CLIENT_FILES["claude"]).stat().st_mode & 0o777 == 0o600,
              oct((home / CLIENT_FILES["claude"]).stat().st_mode & 0o777))

    entry = json.loads((home / CLIENT_FILES["claude"]).read_text())["mcpServers"]["second-cursor"]
    code, out, raw, err = stdio(ctx, [rpc(1, "initialize", INITIALIZE)], env=entry.get("env", {}),
                                command=[entry["command"], *entry["args"]])
    ctx.check("the written stdio entry actually connects to the instance",
              out and out[0].get("result", {}).get("serverInfo", {}).get("name") == "second-cursor", raw[:300] or err)

    hooks = home / HOOKS_FILE
    hooks_original = hooks.read_bytes()
    proc = cli(ctx, "install-mcp", "--client", "cursor", "--with-hook", env=env)
    data = parse_json(hooks.read_text()) or {}
    stop = data.get("hooks", {}).get("stop", [])
    ctx.check("--with-hook: exit 0", proc.returncode == 0, proc.stderr[-300:])
    ctx.check("--with-hook: adds a stop hook that runs --turn-ended",
              len([h for h in stop if "--turn-ended" in h.get("command", "")]) == 1, data)
    ctx.check("--with-hook: existing hooks and version survive",
              data.get("version") == 1 and data.get("hooks", {}).get("preToolUse") == HOOKS["hooks"]["preToolUse"], data)
    ctx.check("--with-hook: hooks.json.bak holds the original", (home / (HOOKS_FILE + ".bak")).read_bytes() == hooks_original)
    cli(ctx, "install-mcp", "--client", "cursor", "--with-hook", env=env)
    stop = (parse_json(hooks.read_text()) or {}).get("hooks", {}).get("stop", [])
    ctx.check("--with-hook: re-running does not duplicate the hook", len(stop) == 1, stop)
    refused = cli(ctx, "install-mcp", "--client", "claude", "--with-hook", env=env)
    ctx.check("--with-hook is refused for clients other than cursor", refused.returncode != 0, refused.stdout + refused.stderr)

    fresh = pathlib.Path(tempfile.mkdtemp(prefix="sc-home-"))
    ctx.cleanups.append(lambda: shutil.rmtree(fresh, ignore_errors=True))
    fresh_env = {"HOME": fresh, "SC_HOME": inst.home}
    for client, rel in CLIENT_FILES.items():
        proc = cli(ctx, "install-mcp", "--client", client, env=fresh_env)
        path = fresh / rel
        ctx.check(f"{client}: missing file and directories are created",
                  proc.returncode == 0 and path.exists() and entry_of(client, load_document(client, path.read_text())),
                  proc.stdout + proc.stderr)
        ctx.check(f"{client}: no .bak when there was nothing to back up", not (fresh / (rel + ".bak")).exists())
    ctx.check("install-mcp --client cursor alone never creates hooks.json", not (fresh / HOOKS_FILE).exists())
    ctx.check("a newly created config holding the token is private (0600)",
              (fresh / CLIENT_FILES["cursor"]).stat().st_mode & 0o777 == 0o600)

    broken = pathlib.Path(tempfile.mkdtemp(prefix="sc-home-"))
    ctx.cleanups.append(lambda: shutil.rmtree(broken, ignore_errors=True))
    settings = broken / CLIENT_FILES["gemini"]
    settings.parent.mkdir(parents=True)
    settings.write_text("{not json")
    proc = cli(ctx, "install-mcp", "--client", "gemini", env={"HOME": broken, "SC_HOME": inst.home})
    ctx.check("invalid existing JSON: refused with a non-zero exit and a reason",
              proc.returncode != 0 and proc.stderr.strip(), proc.stdout + proc.stderr)
    ctx.check("invalid existing JSON: file untouched and no .bak",
              settings.read_text() == "{not json" and not (broken / (CLIENT_FILES["gemini"] + ".bak")).exists())

    check_real_home_untouched(ctx, before, [str(home), str(fresh), inst.token])


# (before our table, our stale table, after it): everything outside the middle must survive byte-for-byte.
CODEX_TABLE_FIXTURES = {
    "quoted and array headers after ours": (
        'model = "o3"\n\n',
        '[mcp_servers.second_cursor]\ncommand = "/old/SecondCursor"\nargs = ["mcp"]\n',
        '\n# trusted projects\n[projects."/tmp/a"]\ntrust_level = "trusted"\n\n'
        '[mcp_servers."x:y"]\ncommand = "xy"\n\n[[hooks.list]]\nname = "first"\n\n[[hooks.list]]\nname = "second"\n',
    ),
    "our header with a trailing comment, a sub-table, then a commented-out header": (
        '[mcp_servers.other]\ncommand = "other-server"\n\n',
        '[mcp_servers.second_cursor]  # managed by Second Cursor\ncommand = "/old/SecondCursor"\nargs = ["mcp"]\n\n'
        '[mcp_servers.second_cursor.env]\nSC_HOME = "/old/home"\n',
        '# [mcp_servers.disabled]\n# command = "off"\n\n[profiles.fast]\nmodel = "gpt-5"\n',
    ),
}


@scenario
def install_mcp_codex_tables(ctx):
    inst = instance(ctx)
    before = real_home_state()
    for label, (prefix, ours, suffix) in CODEX_TABLE_FIXTURES.items():
        home = pathlib.Path(tempfile.mkdtemp(prefix="sc-home-"))
        ctx.cleanups.append(lambda home=home: shutil.rmtree(home, ignore_errors=True))
        path = home / CLIENT_FILES["codex"]
        path.parent.mkdir(parents=True)
        path.write_text(prefix + ours + suffix)
        env = {"HOME": home, "SC_HOME": inst.home}
        expected_rest = tomllib.loads(prefix + suffix)

        first = cli(ctx, "install-mcp", "--client", "codex", env=env)
        text = path.read_text()
        ctx.check(f"{label}: install exits 0", first.returncode == 0, first.stderr[-300:])
        ctx.check(f"{label}: everything before our table is byte-identical", text.startswith(prefix), text)
        ctx.check(f"{label}: everything after our table is byte-identical", text.endswith(suffix), text)
        tables = re.findall(r"(?m)^\s*\[\s*mcp_servers\s*\.\s*[\"']?second_cursor\b", text)
        ctx.check(f"{label}: exactly one second_cursor table, no duplicate or leftover sub-table", len(tables) == 1, text)
        try:
            doc = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            ctx.check(f"{label}: the result is valid TOML", False, f"{e}\n{text}")
            continue
        ctx.check(f"{label}: our entry is current", stdio_entry_ok(entry_of("codex", doc), inst), doc)
        rest = json.loads(json.dumps(doc))
        rest.get("mcp_servers", {}).pop("second_cursor", None)
        ctx.check(f"{label}: every other table parses exactly as before", rest == expected_rest, rest)

        again = cli(ctx, "install-mcp", "--client", "codex", env=env)
        ctx.check(f"{label}: a second re-run exits 0 and changes nothing",
                  again.returncode == 0 and path.read_text() == text, again.stdout + again.stderr)
    check_real_home_untouched(ctx, before, [inst.token])


def config_files(home):
    return sorted(p.name for p in pathlib.Path(home).iterdir() if p.name.startswith("config"))


@scenario
def config_read_only(ctx):
    """CLI commands never create, rewrite or move config.json; only the app replaces an undecodable one, and it
    keeps the old bytes next to it."""
    app = instance(ctx).app
    # A config cut off mid-write: undecodable, but it still holds the user's token and approvals.
    corrupt = b'{\n  "approvedBundleIDs" : ["com.example.kept"],\n  "port" : 1,\n  "token" : "kept-tok'
    other = run.TestInstance(NO_UI, app=app)
    ctx.cleanups.append(other.stop)
    other.config_path.write_bytes(corrupt)
    env = {"SC_HOME": other.home}
    home = pathlib.Path(tempfile.mkdtemp(prefix="sc-home-"))
    ctx.cleanups.append(lambda: shutil.rmtree(home, ignore_errors=True))

    def untouched(label):
        ctx.check(f"{label}: config.json is byte-identical and nothing was moved aside or created",
                  other.config_path.read_bytes() == corrupt and config_files(other.home) == ["config.json"],
                  config_files(other.home))

    proc = cli(ctx, "--turn-ended", env=env, stdin="{}")
    ctx.check("--turn-ended: still exits 0", proc.returncode == 0, proc.stderr)
    ctx.check("--turn-ended: explains on stderr, names the config, prints nothing on stdout",
              "config.json" in proc.stderr and not proc.stdout.strip(), proc.stdout + proc.stderr)
    untouched("--turn-ended")

    code, out, raw, err = stdio(ctx, [rpc(1, "initialize", INITIALIZE)], env=env, timeout=30)
    ctx.check("mcp: fails with a message that names the config", "config.json" in err + raw, err[-300:] or raw[:300])
    untouched("mcp")

    for args in (["--dry-run"], []):
        proc = cli(ctx, "install-mcp", "--client", "cursor", *args, env={"HOME": home, **env})
        ctx.check(f"install-mcp {' '.join(args)}: refused with a non-zero exit that names the config",
                  proc.returncode != 0 and "config.json" in proc.stderr, proc.stdout + proc.stderr)
        untouched(f"install-mcp {' '.join(args)}".strip())
    ctx.check("install-mcp wrote no client config from a broken one", not (home / CLIENT_FILES["cursor"]).exists())

    proc = cli(ctx, "doctor", "--json", env=env)
    report = parse_json(proc.stdout) or {}
    ctx.check("doctor: non-zero, and says the config is unreadable rather than missing",
              proc.returncode != 0 and any("config.json" in p and "not run yet" not in p for p in report.get("problems", [])),
              proc.stdout[:400])
    untouched("doctor")
    ctx.check("the running instance keeps serving with the config it loaded", "permissions" in other.mcp.call("status")[0])

    with TempHome(config=False) as empty:
        down = {"SC_HOME": empty.path}
        proc = cli(ctx, "--turn-ended", env=down, stdin="{}")
        ctx.check("no config: --turn-ended exits 0 and creates none",
                  proc.returncode == 0 and config_files(empty.path) == [], proc.stderr)
        proc = cli(ctx, "install-mcp", "--client", "cursor", "--dry-run", env={"HOME": home, **down})
        ctx.check("no config: install-mcp --client cursor refuses and creates none",
                  proc.returncode != 0 and config_files(empty.path) == [], proc.stdout + proc.stderr)

    with TempHome(config=False) as fresh:
        (fresh.path / "config.json").write_bytes(corrupt)
        logs = fresh.path / "logs"
        logs.mkdir()
        (logs / "stderr.log").touch()
        subprocess.run(["open", "-n", "-g", "--env", f"SC_HOME={fresh.path}", "--stderr", str(logs / "stderr.log"),
                        "-a", str(app)], check=True, capture_output=True)
        aside, replaced, deadline = [], None, time.time() + 20
        while time.time() < deadline and not (aside and replaced):
            time.sleep(0.25)
            aside = list(fresh.path.glob("config.json.invalid-*"))
            replaced = parse_json((fresh.path / "config.json").read_text()) if (fresh.path / "config.json").exists() else None
        # The replacement is a default config (no approvals policy), so stop it before anything could call it.
        if pid := instance_pid(fresh.path):
            run.terminate(pid)
        ctx.check("app launch: the undecodable file is kept byte-for-byte as config.json.invalid-<timestamp>",
                  len(aside) == 1 and aside[0].read_bytes() == corrupt, config_files(fresh.path))
        ctx.check("app launch: a decodable default config took its place",
                  isinstance(replaced, dict) and replaced.get("token") and replaced.get("token") != "kept-tok", replaced)
        log = (logs / "stderr.log").read_text(errors="replace")
        ctx.check("app launch: the log says where the old file went", "config.json.invalid-" in log, log[-400:])


def panel_windows():
    return {w["id"]: w for w in observe()["second_cursor_windows"]}


@scenario
def onboarding_gating(ctx):
    app = instance(ctx).app
    with TempHome() as home:
        before = set(panel_windows())
        launch(app, home.path)
        time.sleep(4)
        appeared = set(panel_windows()) - before
        ctx.check("a test instance (SC_HOME) never opens onboarding on its own", not appeared, appeared)

    if "--ui" not in sys.argv:
        raise Skip("the rest opens the onboarding panel on the user's screen; run with --ui to include it")
    appeared = set()
    with TempHome() as home:
        before = set(panel_windows())
        launch(app, home.path, "--show-onboarding")
        deadline = time.time() + 10
        while time.time() < deadline and not appeared:
            time.sleep(0.5)
            appeared = set(panel_windows()) - before
        ctx.require("--show-onboarding opens exactly one panel, even on a test instance", len(appeared) == 1, appeared)
        panel = next(iter(appeared))
        try:
            tree = ctx.call("get_window_state", window_id=panel, full=True)["tree"]
            ctx.check("panel names the permission and offers the Settings button",
                      "Accessibility" in tree and "Open System Settings" in tree, tree[:600])
            ctx.check("panel exposes the draggable app icon to assistive tech", "Second Cursor app icon" in tree,
                      tree[:600])
            ctx.call("screenshot", window_id=panel)
        except ToolError as e:
            ctx.check("panel contents are readable over MCP", False, e)
    time.sleep(1)
    ctx.check("the panel goes away with its instance", not (set(panel_windows()) & appeared))


if __name__ == "__main__":
    sys.exit(0 if run.Run(config_overrides=NO_UI).run(set(sys.argv[1:])) else 1)
