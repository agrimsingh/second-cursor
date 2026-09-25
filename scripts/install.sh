#!/bin/bash
# Builds, installs to ~/Applications, and relaunches Second Cursor.
set -euo pipefail
cd "$(dirname "$0")/.."

scripts/build-app.sh release
DEST="$HOME/Applications/SecondCursor.app"
mkdir -p "$HOME/Applications"

# SIGTERM makes the running app return hosted windows to the user's screen before exiting;
# give it time, because `open` would otherwise just reactivate the old instance.
if pkill -x SecondCursor 2>/dev/null; then
    for _ in $(seq 1 40); do
        pgrep -x SecondCursor >/dev/null || break
        sleep 0.25
    done
    pkill -9 -x SecondCursor 2>/dev/null || true
fi

rm -rf "$DEST"
ditto build/SecondCursor.app "$DEST"
if [ -x scripts/install-native-host.sh ]; then
    scripts/install-native-host.sh "$DEST/Contents/MacOS/sc-native-host"
fi

# Launch through LaunchServices so macOS attributes permissions to the app, not this shell.
open "$DEST"
echo "Installed $DEST"

CONFIG="$HOME/Library/Application Support/SecondCursor/config.json"
for _ in $(seq 1 40); do
    if [ -f "$CONFIG" ]; then
        PORT="$(plutil -extract port raw -o - "$CONFIG" 2>/dev/null || echo 8787)"
        if nc -z 127.0.0.1 "$PORT" 2>/dev/null; then
            echo "Listening on http://127.0.0.1:$PORT/mcp"
            exit 0
        fi
    fi
    sleep 0.25
done
echo "Second Cursor did not start listening within 10s; check its menu bar status." >&2
exit 1
