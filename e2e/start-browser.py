#!/usr/bin/env python3
"""Starts an isolated, headless, extension-enabled Chrome for Testing profile; never touches user tabs.

The extension reaches the app through sc-native-host, which Chrome launches with its own environment,
so `SC_HOME` in Chrome's environment selects which Second Cursor instance the browser connects to.

Import:  launch(sc_home=..., app=...) -> Browser; Browser.stop() kills Chrome, deletes the profile and
         restores the shared native-messaging manifest. e2e/run.py's TestInstance uses this.
CLI:     e2e/start-browser.py [--sc-home DIR] [--app BUNDLE]
         Prints {"pid", "profile", "log"} and leaves Chrome running; without --sc-home it connects to
         the instance named by the inherited SC_HOME, else the user's app.
"""

import argparse, json, os, pathlib, shutil, signal, subprocess, tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
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


class Browser:
    def __init__(self, process, profile, written, previous):
        self.process, self.pid, self.profile = process, process.pid, profile
        self.log = profile / "browser.log"
        self._written, self._previous = written, previous

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


def launch(sc_home=None, app=None, restore_manifest=True):
    """Starts headless Chrome for Testing with the repo's extension, bridged through `app`'s sc-native-host
    to the instance at `sc_home`."""
    chrome = find_chrome()
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
    process = subprocess.Popen(
        [
            str(chrome),
            "--headless=new",
            "--no-first-run",
            "--no-default-browser-check",
            f"--user-data-dir={profile}",
            f"--load-extension={ROOT}/extension",
            f"--disable-extensions-except={ROOT}/extension",
            "--remote-debugging-port=0",
            "about:blank",
        ],
        stdout=log,
        stderr=log,
        env=env,
        start_new_session=True,
    )
    log.close()
    return Browser(process, profile, written, previous)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sc-home", help="SC_HOME of the Second Cursor instance the extension should reach")
    parser.add_argument("--app", help="app bundle whose sc-native-host Chrome launches (default: ~/Applications)")
    args = parser.parse_args()
    browser = launch(sc_home=args.sc_home, app=args.app, restore_manifest=False)
    print(json.dumps({"pid": browser.pid, "profile": str(browser.profile), "log": str(browser.log)}))
