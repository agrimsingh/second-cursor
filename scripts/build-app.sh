#!/bin/bash
# Builds build/SecondCursor.app and signs it with an existing identity.
# Usage: scripts/build-app.sh [release|debug]
# SC_SIGNING_IDENTITY may name a certificate or its SHA-1 fingerprint.
# Only the default local development identity is created automatically.
set -euo pipefail
cd "$(dirname "$0")/.."

CONFIG="${1:-release}"
IDENTITY="${SC_SIGNING_IDENTITY:-Second Cursor Local Signing}"
APP="build/SecondCursor.app"
case "$CONFIG" in release|debug) ;; *) echo "usage: $0 [release|debug]" >&2; exit 64 ;; esac
if [ $# -gt 1 ]; then echo "usage: $0 [release|debug]" >&2; exit 64; fi
if [ "${SC_SIGNING_IDENTITY+x}" = x ] && [ -z "$SC_SIGNING_IDENTITY" ]; then
    echo "SC_SIGNING_IDENTITY was explicitly set to an empty value." >&2
    exit 1
fi

identity_name() {
    security find-identity -p codesigning | SC_IDENTITY_LOOKUP="$IDENTITY" awk '
        toupper($2) == toupper(ENVIRON["SC_IDENTITY_LOOKUP"]) || index($0, "\"" ENVIRON["SC_IDENTITY_LOOKUP"] "\"") {
            if (match($0, /"[^"]+"/)) { print substr($0, RSTART + 1, RLENGTH - 2); exit }
        }'
}

# A stable identity keeps the Accessibility / Screen Recording grants across rebuilds.
IDENTITY_NAME="$(identity_name)"
if [ -z "$IDENTITY_NAME" ]; then
    if [ "${SC_SIGNING_IDENTITY+x}" = x ]; then
        echo "Requested signing identity was not found: $IDENTITY" >&2
        echo "Use an existing certificate name or SHA-1 fingerprint; no certificate was created." >&2
        exit 1
    fi
    scripts/make-signing-identity.sh
    IDENTITY_NAME="$(identity_name)"
    if [ -z "$IDENTITY_NAME" ]; then echo "Local signing identity is still unavailable." >&2; exit 1; fi
fi

SIGN_OPTIONS=()
HARDENED="${SC_HARDENED_RUNTIME:-0}"
case "$IDENTITY_NAME" in "Developer ID Application:"*) HARDENED=1 ;; esac
case "$HARDENED" in
    1) SIGN_OPTIONS=(--options runtime --timestamp) ;;
    0) ;;
    *) echo "SC_HARDENED_RUNTIME must be 0 or 1." >&2; exit 64 ;;
esac

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
cp Resources/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"
cp Resources/Assets.car "$APP/Contents/Resources/Assets.car"
ditto extension "$APP/Contents/Resources/extension"

# Recent Swift toolchains link compatibility libraries outside the OS runtime.
# Copy those into the app and remove build-machine toolchain search paths, so a
# downloaded app runs without Xcode installed.
mkdir -p "$APP/Contents/Frameworks"
xcrun swift-stdlib-tool --copy --platform macosx \
    --scan-executable "$APP/Contents/MacOS/SecondCursor" \
    --scan-executable "$APP/Contents/MacOS/sc-native-host" \
    --destination "$APP/Contents/Frameworks"
for binary in "$APP/Contents/MacOS/SecondCursor" "$APP/Contents/MacOS/sc-native-host"; do
    while IFS= read -r rpath; do
        case "$rpath" in
            /usr/lib/*|/System/Library/*|@*) ;;
            /*) install_name_tool -delete_rpath "$rpath" "$binary" ;;
        esac
    done < <(otool -l "$binary" | awk '/cmd LC_RPATH/ { getline; getline; sub(/^[[:space:]]*path /, ""); sub(/ \(offset [0-9]+\)$/, ""); print }')
    if ! otool -l "$binary" | awk '/cmd LC_RPATH/ { getline; getline; print $2 }' | grep -Fxq '@executable_path/../Frameworks'; then
        install_name_tool -add_rpath '@executable_path/../Frameworks' "$binary"
    fi
done
for library in "$APP/Contents/Frameworks/"*.dylib; do
    [ -f "$library" ] || continue
    codesign --force -s "$IDENTITY" ${SIGN_OPTIONS[@]+"${SIGN_OPTIONS[@]}"} "$library"
done
codesign --force -s "$IDENTITY" ${SIGN_OPTIONS[@]+"${SIGN_OPTIONS[@]}"} --identifier dev.secondcursor.native-host "$APP/Contents/MacOS/sc-native-host"
codesign --force -s "$IDENTITY" ${SIGN_OPTIONS[@]+"${SIGN_OPTIONS[@]}"} "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"
echo "Built $APP"
