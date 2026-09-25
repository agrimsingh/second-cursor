#!/bin/bash
# Builds build/SecondCursor.app and signs it with the local identity.
# Usage: scripts/build-app.sh [release|debug]
set -euo pipefail
cd "$(dirname "$0")/.."

CONFIG="${1:-release}"
IDENTITY="${SC_SIGNING_IDENTITY:-Second Cursor Local Signing}"
APP="build/SecondCursor.app"

# A stable identity keeps the Accessibility / Screen Recording grants across rebuilds.
if ! security find-identity -p codesigning | grep -q "\"$IDENTITY\""; then
    scripts/make-signing-identity.sh
fi

SCRATCH=()
if [ -n "${SC_SCRATCH_PATH:-}" ]; then SCRATCH=(--scratch-path "$SC_SCRATCH_PATH"); fi

# macOS ships bash 3.2, where "${SCRATCH[@]}" on an empty array trips `set -u`.
swift build ${SCRATCH[@]+"${SCRATCH[@]}"} -c "$CONFIG" --product SecondCursor
swift build ${SCRATCH[@]+"${SCRATCH[@]}"} -c "$CONFIG" --product sc-native-host
BIN="$(swift build ${SCRATCH[@]+"${SCRATCH[@]}"} -c "$CONFIG" --show-bin-path)"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN/SecondCursor" "$APP/Contents/MacOS/SecondCursor"
cp "$BIN/sc-native-host" "$APP/Contents/MacOS/sc-native-host"
cp Resources/Info.plist "$APP/Contents/Info.plist"

codesign --force -s "$IDENTITY" --identifier dev.secondcursor.native-host "$APP/Contents/MacOS/sc-native-host"
codesign --force -s "$IDENTITY" "$APP"
echo "Built $APP"
