#!/usr/bin/env python3
"""Starts isolated, extension-enabled Chrome for Testing; headless by default, --headed shows its window.

The extension reaches the app through sc-native-host, which Chrome launches with its own environment,
so `SC_HOME` in Chrome's environment selects which Second Cursor instance the browser connects to.

Import:  launch(sc_home=..., app=...) -> Browser; Browser.stop() kills Chrome, deletes the profile and
         restores the shared native-messaging manifest. e2e/run.py's TestInstance uses this.
CLI:     e2e/start-browser.py [--sc-home DIR] [--app BUNDLE] [--headed]
         Prints {"pid", "profile", "log"} and leaves Chrome running; without --sc-home it connects to
         the instance named by the inherited SC_HOME, else the user's app.
Env:     SC_E2E_EXTENSION=DIR loads the extension from DIR instead of the repo's extension/ (a saved
         baseline, for before/after timing runs).
"""

import argparse, hashlib, json, os, pathlib, shutil, signal, subprocess, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
EXTENSION = pathlib.Path(os.environ.get("SC_E2E_EXTENSION") or ROOT / "extension").expanduser().resolve()
INSTALLED_APP = pathlib.Path.home() / "Applications/SecondCursor.app"
HOST_NAME = "dev.secondcursor.host"
EXTENSION_ID = "nfjjbnelcophiemlajgbmahaejppblog"
CHROME = "Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
# Chrome for Testing looks up native messaging manifests in its standard support directory, which every
# profile shares with the user's own registration (scripts/install-native-host.sh).
SHARED_MANIFEST = (
    pathlib.Path.home()
    / f"Library/Application Support/Google/Chrome for Testing/NativeMessagingHosts/{HOST_NAME}.json"
)


def find_chrome():
    home = pathlib.Path.home()
    choices = [
        *(home / "Library/Caches/ms-playwright").glob(f"chromium-*/chrome-mac*/{CHROME}"),
        *(home / ".agent-browser/browsers").glob(f"*/{CHROME}"),
    ]
    if not choices:
        raise RuntimeError(
            "Chrome for Testing not found in ~/Library/Caches/ms-playwright or ~/.agent-browser/browsers; "
            "install Playwright Chromium or agent-browser's Chrome first"
        )
    return max(choices, key=lambda p: p.stat().st_mtime)


def same_manifest(a, b):
    try:
        return a is not None and json.loads(a) == json.loads(b)
    except ValueError:
        return False


def browser_identity(executable=None):
    """Pin the executable actually launched; inspecting --version never opens a browser window."""
    chrome = pathlib.Path(executable or find_chrome()).expanduser().resolve()
    result = subprocess.run([str(chrome), "--version"], capture_output=True, text=True,
                            check=True, timeout=5)
    version = result.stdout.strip()
    if not version:
        raise RuntimeError(f"Chrome for Testing did not report a version: {chrome}")
    digest = hashlib.sha256()
    with chrome.open("rb") as binary:
        for chunk in iter(lambda: binary.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"binary": str(chrome), "version": version, "sha256": digest.hexdigest()}


def launch_command(chrome, profile, headed=False):
    """Keep headed and headless runs identical apart from whether Chrome creates a visible window."""
    return [
        str(chrome),
        *([] if headed else ["--headless=new"]),
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={profile}",
        f"--load-extension={EXTENSION}",
        f"--disable-extensions-except={EXTENSION}",
        "--remote-debugging-port=0",
        "about:blank",
    ]


class Browser:
    def __init__(self, process, profile, written, previous, identity=None, headed=False):
        self.process, self.pid, self.profile = process, process.pid, profile
        self.log = profile / "browser.log"
        self._written, self._previous = written, previous
        self.identity = dict(identity or {})
        self.headed = bool(headed)
        self.executable, self.version = self.identity.get("binary"), self.identity.get("version")

    @property
    def metadata(self):
        return {"identity": dict(self.identity), "headed": self.headed}

    def running(self):
        return self.process.poll() is None

    def stop(self):
        # Chrome was started in its own session, so its pid is also its helpers' process group.
        for sig, seconds in ((signal.SIGTERM, 5), (signal.SIGKILL, 2)):
            if not self.running():
                break
            try:
                os.killpg(self.pid, sig)
                self.process.wait(seconds)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
        try:
            os.killpg(self.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        if self._written is not None and SHARED_MANIFEST.exists() and SHARED_MANIFEST.read_bytes() == self._written:
            if self._previous is None:
                SHARED_MANIFEST.unlink()
            else:
                SHARED_MANIFEST.write_bytes(self._previous)
        self._written = None
        shutil.rmtree(self.profile, ignore_errors=True)


def launch(sc_home=None, app=None, restore_manifest=True, headed=False):
    """Start isolated Chrome for Testing with the extension bridged to `sc_home`; opt into a visible window
    with `headed=True`. Both modes use a disposable profile and the same browser executable."""
    chrome = find_chrome()
    identity = browser_identity(chrome)
    host = pathlib.Path(app or INSTALLED_APP).expanduser().resolve() / "Contents/MacOS/sc-native-host"
    if not os.access(host, os.X_OK):
        raise RuntimeError(f"native host not found: {host}")
    manifest = json.dumps(
        {
            "name": HOST_NAME,
            "description": "Second Cursor browser bridge",
            "path": str(host),
            "type": "stdio",
            "allowed_origins": [f"chrome-extension://{EXTENSION_ID}/"],
        },
        indent=2,
    ).encode()
    profile = pathlib.Path(tempfile.mkdtemp(prefix="second-cursor-browser-"))
    (profile / "NativeMessagingHosts").mkdir()
    (profile / f"NativeMessagingHosts/{HOST_NAME}.json").write_bytes(manifest)
    previous = SHARED_MANIFEST.read_bytes() if SHARED_MANIFEST.exists() else None
    written = None
    if not same_manifest(previous, manifest):
        SHARED_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        SHARED_MANIFEST.write_bytes(manifest)
        written = manifest if restore_manifest else None
    (profile / "Default").mkdir()
    (profile / "downloads").mkdir()
    (profile / "Default/Preferences").write_text(
        json.dumps(
            {
                "download": {
                    "default_directory": str(profile / "downloads"),
                    "prompt_for_download": False,
                }
            }
        )
    )
    env = dict(os.environ)
    if sc_home:
        env["SC_HOME"] = str(sc_home)
    log = open(profile / "browser.log", "w")
    try:
        process = subprocess.Popen(
            launch_command(chrome, profile, headed=headed),
            stdout=log,
            stderr=log,
            env=env,
            start_new_session=True,
        )
    except BaseException:
        # A failed spawn owns no process, but must still undo its temporary native-host registration.
        if not same_manifest(previous, manifest) and SHARED_MANIFEST.exists() and SHARED_MANIFEST.read_bytes() == manifest:
            if previous is None:
                SHARED_MANIFEST.unlink()
            else:
                SHARED_MANIFEST.write_bytes(previous)
        shutil.rmtree(profile, ignore_errors=True)
        raise
    finally:
        log.close()
    return Browser(process, profile, written, previous, identity=identity, headed=headed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sc-home", help="SC_HOME of the Second Cursor instance the extension should reach")
    parser.add_argument("--app", help="app bundle whose sc-native-host Chrome launches (default: ~/Applications)")
    parser.add_argument("--headed", action="store_true", help="show the isolated Chrome window (default: headless)")
    args = parser.parse_args()
    browser = launch(sc_home=args.sc_home, app=args.app, restore_manifest=False, headed=args.headed)
    print(json.dumps({"pid": browser.pid, "profile": str(browser.profile), "log": str(browser.log),
                      **browser.metadata}))
