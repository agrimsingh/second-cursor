#!/bin/bash
# Package a self-contained app. This script never creates signing credentials,
# installs/launches the app, or publishes artifacts.
set -euo pipefail
cd "$(dirname "$0")/.."

usage() {
    cat <<'EOF'
Usage: scripts/package-release.sh [--check] [--skip-build] [--notarize] [--output DIRECTORY]

By default, build and package locally signed ZIP + DMG artifacts.
  --check       Read-only readiness checks; do not build, sign, or package.
  --skip-build  Package the prepared build/SecondCursor.app.
  --notarize    Require Developer ID signing and notarize with Apple.
  --output DIR  Output directory (default: build/release).

SC_SIGNING_IDENTITY  Existing certificate name or SHA-1 fingerprint.
                    Default for local builds: Second Cursor Local Signing.
SC_NOTARY_PROFILE   Existing notarytool keychain profile, required by --notarize.
SC_SCRATCH_PATH     Optional Swift build scratch directory.
EOF
}
fail() { echo "error: $*" >&2; exit 1; }
CHECK=0
SKIP_BUILD=0
NOTARIZE=0
OUTPUT="build/release"
while [ $# -gt 0 ]; do
    case "$1" in
        --check) CHECK=1 ;;
        --skip-build) SKIP_BUILD=1 ;;
        --notarize) NOTARIZE=1 ;;
        --output)
            [ $# -ge 2 ] && [ -n "$2" ] || fail "--output requires a directory"
            case "$2" in -*) fail "--output requires a directory, not another option (use ./-name for a directory beginning with '-')" ;; esac
            OUTPUT="$2"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; fail "Unknown argument: $1" ;;
    esac
    shift
done
case "$OUTPUT" in /*) ;; *) OUTPUT="$PWD/$OUTPUT" ;; esac
APP="$PWD/build/SecondCursor.app"
IDENTITY="${SC_SIGNING_IDENTITY:-Second Cursor Local Signing}"
if [ "${SC_SIGNING_IDENTITY+x}" = x ] && [ -z "$SC_SIGNING_IDENTITY" ]; then
    fail "SC_SIGNING_IDENTITY was explicitly set to an empty value"
fi

for command in security codesign ditto hdiutil plutil shasum otool lipo awk; do
    command -v "$command" >/dev/null || fail "Required command is unavailable: $command"
done
if [ "$SKIP_BUILD" -eq 0 ]; then
    command -v swift >/dev/null || fail "Building requires Swift; use --skip-build for a prepared app"
    command -v install_name_tool >/dev/null || fail "Building requires install_name_tool"
    xcrun --find swift-stdlib-tool >/dev/null || fail "Building requires swift-stdlib-tool to bundle compatibility libraries"
fi

# The unfiltered list includes self-signed development certificates. The release
# path separately requires the certificate to appear in the valid identity list.
identity_match() {
    SC_IDENTITY_LOOKUP="$IDENTITY" awk '
        toupper($2) == toupper(ENVIRON["SC_IDENTITY_LOOKUP"]) || index($0, "\"" ENVIRON["SC_IDENTITY_LOOKUP"] "\"") {
            if (match($0, /"[^"]+"/)) { print substr($0, RSTART + 1, RLENGTH - 2); exit }
        }'
}
identity_hash() {
    SC_IDENTITY_LOOKUP="$IDENTITY" awk '
        toupper($2) == toupper(ENVIRON["SC_IDENTITY_LOOKUP"]) || index($0, "\"" ENVIRON["SC_IDENTITY_LOOKUP"] "\"") { print $2; exit }'
}
IDENTITY_NAME="$(security find-identity -p codesigning | identity_match)"
IDENTITY_HASH="$(security find-identity -v -p codesigning | identity_hash)"
if [ -z "$IDENTITY_HASH" ]; then IDENTITY_HASH="$(security find-identity -p codesigning | identity_hash)"; fi
if [ "$SKIP_BUILD" -eq 0 ] || [ "$NOTARIZE" -eq 1 ] || [ "${SC_SIGNING_IDENTITY+x}" = x ]; then
    [ -n "$IDENTITY_NAME" ] || fail "Signing identity not found: $IDENTITY. Provision it separately; the packager never creates credentials."
fi
if [ "$NOTARIZE" -eq 1 ]; then
    case "$IDENTITY_NAME" in
        "Developer ID Application:"*) ;;
        *) fail "--notarize requires an existing Developer ID Application identity in SC_SIGNING_IDENTITY" ;;
    esac
    [ -n "$(security find-identity -v -p codesigning | identity_match)" ] || fail "Requested Developer ID identity is not currently valid"
    [ -n "${SC_NOTARY_PROFILE:-}" ] || fail "--notarize requires SC_NOTARY_PROFILE naming an existing notarytool keychain profile"
    xcrun --find notarytool >/dev/null || fail "notarytool is unavailable"
    xcrun --find stapler >/dev/null || fail "stapler is unavailable"
    command -v spctl >/dev/null || fail "spctl is unavailable"
fi

plist_value() { /usr/libexec/PlistBuddy -c "Print :$2" "$1"; }
validate_version() {
    case "$1" in ''|*[!0-9A-Za-z._-]*) fail "Unsafe or missing $2 in app Info.plist" ;; esac
}
validate_binary() {
    local binary="$1" bundle="$2" dependencies dependency rpath
    [ -x "$binary" ] || fail "Missing executable: $binary"
    dependencies="$(otool -L "$binary")" || fail "Cannot inspect binary: $binary"
    while IFS= read -r dependency; do
        case "$dependency" in
            /System/Library/*|/usr/lib/*) ;;
            @rpath/libswift*.dylib)
                [ -f "$bundle/Contents/Frameworks/${dependency#@rpath/}" ] || fail "Unbundled Swift runtime dependency in $binary: $dependency" ;;
            '') ;;
            *) fail "Non-system runtime dependency in $binary: $dependency" ;;
        esac
    done < <(printf '%s\n' "$dependencies" | awk '/^[[:space:]]/ { print $1 }')
    while IFS= read -r rpath; do
        case "$rpath" in
            /usr/lib/*|/System/Library/*|@*) ;;
            *) fail "Nonportable runtime search path in $binary: $rpath" ;;
        esac
    done < <(otool -l "$binary" | awk '/cmd LC_RPATH/ { getline; getline; sub(/^[[:space:]]*path /, ""); sub(/ \(offset [0-9]+\)$/, ""); print }')
    lipo -archs "$binary" >/dev/null || fail "Not a supported Mach-O executable: $binary"
}
validate_app() {
    local bundle="$1" executable authority details helper_archs app_archs
    [ -f "$bundle/Contents/Info.plist" ] || fail "Prepared app is missing Info.plist: $bundle"
    plutil -lint "$bundle/Contents/Info.plist" >/dev/null
    [ "$(plist_value "$bundle/Contents/Info.plist" CFBundleIdentifier)" = dev.secondcursor.app ] || fail "Unexpected app bundle identifier"
    executable="$(plist_value "$bundle/Contents/Info.plist" CFBundleExecutable)"
    [ "$executable" = SecondCursor ] || fail "Unexpected app executable: $executable"
    [ -f "$bundle/Contents/Resources/extension/manifest.json" ] || fail "The prepared app is missing its bundled browser extension; rebuild it"
    [ "$(plutil -extract manifest_version raw -o - "$bundle/Contents/Resources/extension/manifest.json")" = 3 ] || fail "The bundled extension manifest is invalid or unsupported"
    # All extension JS modules ship together; reject a partial resource copy.
    for source in extension/*.js; do
        [ -s "$bundle/Contents/Resources/$source" ] || fail "The prepared app is missing $source"
    done
    validate_binary "$bundle/Contents/MacOS/$executable" "$bundle"
    validate_binary "$bundle/Contents/MacOS/sc-native-host" "$bundle"
    for library in "$bundle/Contents/Frameworks/"*.dylib; do
        [ -f "$library" ] || continue
        validate_binary "$library" "$bundle"
        codesign --verify --strict --verbose=2 "$library"
    done
    app_archs="$(lipo -archs "$bundle/Contents/MacOS/$executable")"
    helper_archs="$(lipo -archs "$bundle/Contents/MacOS/sc-native-host")"
    [ "$app_archs" = "$helper_archs" ] || fail "The app and helper architectures differ"
    codesign --verify --deep --strict --verbose=2 "$bundle"
    codesign --verify --strict --verbose=2 "$bundle/Contents/MacOS/sc-native-host"
    if [ "$NOTARIZE" -eq 1 ] || [ "${SC_SIGNING_IDENTITY+x}" = x ]; then
        for binary in "$bundle" "$bundle/Contents/MacOS/sc-native-host"; do
            details="$(codesign -d --verbose=4 "$binary" 2>&1)"
            authority="$(printf '%s\n' "$details" | awk '/^Authority=/ { sub(/^Authority=/, ""); print; exit }')"
            [ "$authority" = "$IDENTITY_NAME" ] || fail "Prepared signature does not match requested signing identity: $binary"
            codesign --verify --strict -R="certificate leaf = H\"$IDENTITY_HASH\"" "$binary"
            if [ "$NOTARIZE" -eq 1 ]; then
                printf '%s\n' "$details" | grep -q 'flags=.*runtime' || fail "Hardened runtime is missing: $binary"
                printf '%s\n' "$details" | grep -q '^Timestamp=' || fail "Secure signing timestamp is missing: $binary"
            fi
        done
    fi
}

INFO="Resources/Info.plist"
if [ "$SKIP_BUILD" -eq 1 ]; then INFO="$APP/Contents/Info.plist"; fi
[ -f "$INFO" ] || fail "App metadata is unavailable: $INFO"
VERSION="$(plist_value "$INFO" CFBundleShortVersionString)"
BUILD="$(plist_value "$INFO" CFBundleVersion)"
validate_version "$VERSION" CFBundleShortVersionString
validate_version "$BUILD" CFBundleVersion
BASE="SecondCursor-$VERSION-$BUILD"
if [ "$NOTARIZE" -eq 1 ]; then BASE="$BASE-notarized"; else BASE="$BASE-local"; fi
for suffix in zip dmg release.json sha256 app-notary.json dmg-notary.json; do
    [ ! -e "$OUTPUT/$BASE.$suffix" ] || fail "Output already exists: $OUTPUT/$BASE.$suffix"
done
PARENT="$OUTPUT"
while [ ! -e "$PARENT" ]; do PARENT="$(dirname "$PARENT")"; done
[ -d "$PARENT" ] && [ -w "$PARENT" ] || fail "Output parent is not a writable directory: $PARENT"
if [ "$SKIP_BUILD" -eq 1 ]; then validate_app "$APP"; fi
if [ "$CHECK" -eq 1 ]; then
    echo "Readiness checks passed for $BASE. No files or credentials were changed."
    if [ "$NOTARIZE" -eq 1 ]; then
        echo "Notary profile is configured; Apple credentials/service access are verified only during submission."
    fi
    exit 0
fi

mkdir -p "$OUTPUT"
LOCK="$OUTPUT/.$BASE.lock"
mkdir "$LOCK" 2>/dev/null || fail "Another packager owns $LOCK; if a previous run was killed, remove only this stale lock after checking no packager is running"
WORK=""
cleanup() {
    if [ -n "$WORK" ]; then rm -rf "$WORK"; fi
    rmdir "$LOCK" 2>/dev/null || true
}
trap cleanup EXIT
# Check again after acquiring the lock; another run may have finished during
# readiness checking. Never let mv -n silently leave an older artifact in place.
for suffix in zip dmg release.json sha256 app-notary.json dmg-notary.json; do
    [ ! -e "$OUTPUT/$BASE.$suffix" ] || fail "Output already exists: $OUTPUT/$BASE.$suffix"
done
if [ "$SKIP_BUILD" -eq 0 ]; then
    # Always pass an explicit identity, preventing build-app's default development
    # certificate creation branch from running in this packaging workflow.
    SC_SIGNING_IDENTITY="$IDENTITY_HASH" SC_HARDENED_RUNTIME="$NOTARIZE" scripts/build-app.sh release
    validate_app "$APP"
fi
WORK="$(mktemp -d "$OUTPUT/.package.XXXXXX")"
mkdir "$WORK/disk"
STAGED_APP="$WORK/disk/SecondCursor.app"
ditto "$APP" "$STAGED_APP"

retain_failure() {
    local failure_dir
    failure_dir="$(mktemp -d "$OUTPUT/$BASE-failed.XXXXXX")"
    for diagnostic in "$WORK"/*-notary.json "$WORK"/*-notary.log; do
        [ ! -f "$diagnostic" ] || cp "$diagnostic" "$failure_dir/"
    done
    echo "Notarization diagnostics: $failure_dir" >&2
}
notarize_file() {
    local artifact="$1" label="$2" response status submission submitted
    response="$WORK/$label-notary.json"
    submitted=1
    xcrun notarytool submit "$artifact" --keychain-profile "$SC_NOTARY_PROFILE" --wait --output-format json > "$response" 2> "$WORK/$label-notary.log" || submitted=0
    status="$(plutil -extract status raw -o - "$response" 2>/dev/null || true)"
    if [ "$submitted" -eq 0 ] || [ "$status" != Accepted ]; then
        submission="$(plutil -extract id raw -o - "$response" 2>/dev/null || true)"
        if [ -n "$submission" ]; then
            xcrun notarytool log "$submission" --keychain-profile "$SC_NOTARY_PROFILE" "$WORK/$label-service-notary.log" || true
        fi
        retain_failure
        fail "Apple did not accept $label (status: ${status:-unknown})"
    fi
}

if [ "$NOTARIZE" -eq 1 ]; then
    ditto -c -k --sequesterRsrc --keepParent "$STAGED_APP" "$WORK/app-submission.zip"
    notarize_file "$WORK/app-submission.zip" app
    xcrun stapler staple "$STAGED_APP"
    xcrun stapler validate "$STAGED_APP"
    codesign --verify --deep --strict --verbose=2 "$STAGED_APP"
    spctl --assess --type execute --verbose=2 "$STAGED_APP"
fi

ditto -c -k --sequesterRsrc --keepParent "$STAGED_APP" "$WORK/$BASE.zip"
# Verify the final archive, including the stapled ticket on the notarized path.
mkdir "$WORK/unzip"
ditto -x -k "$WORK/$BASE.zip" "$WORK/unzip"
validate_app "$WORK/unzip/SecondCursor.app"
if [ "$NOTARIZE" -eq 1 ]; then xcrun stapler validate "$WORK/unzip/SecondCursor.app"; fi
ln -s /Applications "$WORK/disk/Applications"
hdiutil create -quiet -volname "Second Cursor $VERSION" -srcfolder "$WORK/disk" -format UDZO "$WORK/$BASE.dmg"
hdiutil verify -quiet "$WORK/$BASE.dmg"
if [ "$NOTARIZE" -eq 1 ]; then
    codesign --force --sign "$IDENTITY_HASH" --timestamp "$WORK/$BASE.dmg"
    codesign --verify --strict --verbose=2 "$WORK/$BASE.dmg"
    notarize_file "$WORK/$BASE.dmg" dmg
    xcrun stapler staple "$WORK/$BASE.dmg"
    xcrun stapler validate "$WORK/$BASE.dmg"
    spctl --assess --type open --context context:primary-signature --verbose=2 "$WORK/$BASE.dmg"
fi
hdiutil verify -quiet "$WORK/$BASE.dmg"
ZIP_SHA="$(shasum -a 256 "$WORK/$BASE.zip" | awk '{ print $1 }')"
DMG_SHA="$(shasum -a 256 "$WORK/$BASE.dmg" | awk '{ print $1 }')"
printf '%s  %s\n%s  %s\n' "$ZIP_SHA" "$BASE.zip" "$DMG_SHA" "$BASE.dmg" > "$WORK/$BASE.sha256"

MANIFEST="$WORK/$BASE.release.json"
plutil -create xml1 "$MANIFEST"
manifest_string() { plutil -insert "$1" -string "$2" "$MANIFEST"; }
manifest_bool() { plutil -insert "$1" -bool "$2" "$MANIFEST"; }
plutil -insert schema_version -integer 1 "$MANIFEST"
manifest_string app_identifier dev.secondcursor.app
manifest_string version "$VERSION"
manifest_string build "$BUILD"
manifest_string minimum_macos "$(plist_value "$STAGED_APP/Contents/Info.plist" LSMinimumSystemVersion)"
manifest_string architectures "$(lipo -archs "$STAGED_APP/Contents/MacOS/SecondCursor")"
manifest_string generated_at "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
SIGNATURE="$(codesign -d --verbose=4 "$STAGED_APP" 2>&1)"
AUTHORITY="$(printf '%s\n' "$SIGNATURE" | awk '/^Authority=/ { sub(/^Authority=/, ""); print; exit }')"
manifest_string signing_authority "${AUTHORITY:-ad-hoc}"
if [ "$NOTARIZE" -eq 1 ]; then
    manifest_string distribution notarized_developer_id
    manifest_bool notarized true
    manifest_bool app_staple_verified true
    manifest_bool dmg_staple_verified true
    manifest_bool gatekeeper_verified true
else
    manifest_string distribution local_unnotarized
    manifest_bool notarized false
    manifest_bool app_staple_verified false
    manifest_bool dmg_staple_verified false
    manifest_bool gatekeeper_verified false
fi
manifest_bool app_signature_verified true
manifest_bool helper_signature_verified true
if [ "$NOTARIZE" -eq 1 ]; then manifest_bool dmg_signature_verified true; else manifest_bool dmg_signature_verified false; fi
manifest_bool archive_verified true
manifest_bool disk_image_verified true
manifest_string zip_file "$BASE.zip"
manifest_string zip_sha256 "$ZIP_SHA"
manifest_string dmg_file "$BASE.dmg"
manifest_string dmg_sha256 "$DMG_SHA"
plutil -convert json "$MANIFEST"

# Publish the manifest last: its presence marks a fully verified package set.
for suffix in zip dmg sha256; do ln "$WORK/$BASE.$suffix" "$OUTPUT/$BASE.$suffix"; done
if [ "$NOTARIZE" -eq 1 ]; then
    ln "$WORK/app-notary.json" "$OUTPUT/$BASE.app-notary.json"
    ln "$WORK/dmg-notary.json" "$OUTPUT/$BASE.dmg-notary.json"
fi
ln "$MANIFEST" "$OUTPUT/$BASE.release.json"
echo "Packaged $OUTPUT/$BASE.zip"
echo "Packaged $OUTPUT/$BASE.dmg"
echo "Release manifest: $OUTPUT/$BASE.release.json"
if [ "$NOTARIZE" -eq 0 ]; then echo "Local package only: these artifacts are not notarized or Gatekeeper-approved for distribution."; fi
