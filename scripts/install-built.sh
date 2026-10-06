#!/bin/bash
# Install an already signed and tested bundle, including its bundled extension.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ $# -ne 1 ] || [ ! -d "$1" ]; then
    echo "usage: scripts/install-built.sh <SecondCursor.app>" >&2
    exit 64
fi
SOURCE_APP="$(cd "$1" && pwd -P)"
DEST="$HOME/Applications/SecondCursor.app"
if [ "$SOURCE_APP" = "$DEST" ]; then
    echo "The source bundle is already installed." >&2
    exit 64
fi
if [ "$(plutil -extract CFBundleIdentifier raw -o - "$SOURCE_APP/Contents/Info.plist")" != dev.secondcursor.app ]; then
    echo "The source is not a Second Cursor application bundle." >&2
    exit 64
fi
codesign --verify --deep --strict "$SOURCE_APP"
mkdir -p "$HOME/Applications" build/install-backups
BACKUP="$(mktemp -d "$PWD/build/install-backups/install-XXXXXXXX")"
ditto "$SOURCE_APP" "$BACKUP/candidate.app"
codesign --verify --deep --strict "$BACKUP/candidate.app"

# Preserve hosted windows by allowing normal termination. Never force-kill an active task.
if pkill -x SecondCursor 2>/dev/null; then
    for _ in $(seq 1 50); do pgrep -x SecondCursor >/dev/null || break; sleep 0.2; done
fi
if pgrep -x SecondCursor >/dev/null; then
    echo "The existing app did not exit gracefully; nothing was replaced." >&2
    exit 1
fi

REPLACEMENT_STARTED=0
HAD_PREVIOUS=0
[ ! -e "$DEST" ] || HAD_PREVIOUS=1
rollback() {
    result=$?
    if [ "$result" -ne 0 ] && [ "$REPLACEMENT_STARTED" -eq 1 ]; then
        echo "Installation failed; restoring the previous bundle from $BACKUP." >&2
        pkill -x SecondCursor 2>/dev/null || true
        for _ in $(seq 1 50); do pgrep -x SecondCursor >/dev/null || break; sleep 0.2; done
        if pgrep -x SecondCursor >/dev/null; then
            echo "App still running; the previous bundle is preserved at $BACKUP/previous.app." >&2
        else
            if [ -d "$BACKUP/previous.app" ]; then
                if [ -e "$DEST" ]; then mv "$DEST" "$BACKUP/failed.app"; fi
                mv "$BACKUP/previous.app" "$DEST"
                "$DEST/Contents/MacOS/SecondCursor" install-browser || true
                open "$DEST" || true
            elif [ "$HAD_PREVIOUS" -eq 0 ] && [ -e "$DEST" ]; then
                mv "$DEST" "$BACKUP/failed.app"
            fi
        fi
    fi
    exit "$result"
}
trap rollback EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
REPLACEMENT_STARTED=1
if [ -e "$DEST" ]; then mv "$DEST" "$BACKUP/previous.app"; fi
mv "$BACKUP/candidate.app" "$DEST"
# Use the installed bundle, not possibly newer working-tree extension sources.
"$DEST/Contents/MacOS/SecondCursor" install-browser
open "$DEST"
READY=0
for _ in $(seq 1 40); do
    if "$DEST/Contents/MacOS/SecondCursor" doctor --json > "$BACKUP/doctor.json" 2>/dev/null; then
        READY=1
        break
    fi
    sleep 0.25
done
if [ "$READY" -ne 1 ]; then echo "The installed app did not become ready." >&2; exit 1; fi
codesign --verify --deep --strict "$DEST"
echo "Installed $DEST"
echo "Previous bundle and startup diagnostics: $BACKUP"
shasum -a 256 "$DEST/Contents/MacOS/SecondCursor"
