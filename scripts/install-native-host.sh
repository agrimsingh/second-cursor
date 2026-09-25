#!/bin/bash
# Registers sc-native-host with each installed Chromium-family browser.
# Usage: scripts/install-native-host.sh <host-binary-path> [chrome-user-data-dir ...]
# Extra user-data dirs are for browsers launched with --user-data-dir, which read
# NativeMessagingHosts from that dir instead of the default one.
set -euo pipefail

HOST_NAME="dev.secondcursor.host"
EXTENSION_ID="nfjjbnelcophiemlajgbmahaejppblog"

if [ $# -lt 1 ]; then
    echo "usage: $0 <host-binary-path> [chrome-user-data-dir ...]" >&2
    exit 64
fi
BINARY="$1"
shift
if [ ! -x "$BINARY" ]; then
    echo "not an executable: $BINARY" >&2
    exit 1
fi
BINARY="$(cd "$(dirname "$BINARY")" && pwd)/$(basename "$BINARY")"
ESCAPED="${BINARY//\\/\\\\}"
ESCAPED="${ESCAPED//\"/\\\"}"

SUPPORT="$HOME/Library/Application Support"

# Chrome loads unpacked extensions in place; a stable path outside ~/Downloads avoids
# Chrome's Downloads-folder permission prompt and survives the repo moving.
REPO="$(cd "$(dirname "$0")/.." && pwd)"
EXTENSION_DEST="$SUPPORT/SecondCursor/extension"
mkdir -p "$SUPPORT/SecondCursor"
rm -rf "$EXTENSION_DEST"
cp -R "$REPO/extension" "$EXTENSION_DEST"
echo "Extension copied to $EXTENSION_DEST (load this folder via chrome://extensions > Load unpacked)"

DIRS=()
for browser in "Google/Chrome" "Google/Chrome for Testing" "Chromium" "BraveSoftware/Brave-Browser" "Microsoft Edge" "Microsoft Edge Beta"; do
    if [ -d "$SUPPORT/$browser" ]; then
        DIRS+=("$SUPPORT/$browser")
    fi
done
DIRS+=("$@")

if [ ${#DIRS[@]} -eq 0 ]; then
    echo "No Chrome, Chrome for Testing, Chromium, or Brave profile found; nothing installed." >&2
    exit 0
fi

for dir in "${DIRS[@]}"; do
    mkdir -p "$dir/NativeMessagingHosts"
    manifest="$dir/NativeMessagingHosts/$HOST_NAME.json"
    cat > "$manifest" <<EOF
{
  "name": "$HOST_NAME",
  "description": "Second Cursor browser bridge",
  "path": "$ESCAPED",
  "type": "stdio",
  "allowed_origins": ["chrome-extension://$EXTENSION_ID/"]
}
EOF
    echo "Installed $manifest"
done
