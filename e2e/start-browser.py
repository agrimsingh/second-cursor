#!/usr/bin/env python3
"""Starts an isolated extension-enabled Chromium profile; never touches user tabs."""

import pathlib, subprocess, tempfile, json, time, os, sys

root = pathlib.Path(__file__).resolve().parents[1]
choices = sorted(
    (pathlib.Path.home() / "Library/Caches/ms-playwright").glob(
        "chromium-*/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
    )
)
if not choices:
    raise SystemExit("Install Chrome for Testing or Playwright Chromium first")
profile = pathlib.Path(tempfile.mkdtemp(prefix="second-cursor-browser-"))
host = (
    pathlib.Path.home() / "Applications/SecondCursor.app/Contents/MacOS/sc-native-host"
)
subprocess.run(
    [str(root / "scripts/install-native-host.sh"), str(host), str(profile)],
    check=True,
    stdout=subprocess.DEVNULL,
)
# On macOS native messaging manifests are looked up in the standard browser support directory.
standard = (
    pathlib.Path.home()
    / "Library/Application Support/Google/Chrome for Testing/NativeMessagingHosts"
)
standard.mkdir(parents=True, exist_ok=True)
manifest = profile / "NativeMessagingHosts/dev.secondcursor.host.json"
(standard / manifest.name).write_bytes(manifest.read_bytes())
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
log = open(profile / "browser.log", "w")
process = subprocess.Popen(
    [
        str(choices[-1]),
        "--headless=new",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={profile}",
        f"--load-extension={root}/extension",
        f"--disable-extensions-except={root}/extension",
        "--remote-debugging-port=0",
        "about:blank",
    ],
    stdout=log,
    stderr=log,
    start_new_session=True,
)
print(
    json.dumps(
        {
            "pid": process.pid,
            "profile": str(profile),
            "log": str(profile / "browser.log"),
        }
    )
)
