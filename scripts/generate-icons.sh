#!/bin/bash
# Regenerate the macOS and browser icons from the original artwork.
# The generated assets are checked in, so ordinary builds need no image tooling.
set -euo pipefail
cd "$(dirname "$0")/.."

SOURCE="Resources/Brand/AppIcon.png"
[ -f "$SOURCE" ] || { echo "Missing icon artwork: $SOURCE" >&2; exit 1; }
WORK="$(mktemp -d "${TMPDIR:-/tmp}/second-cursor-icons.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
ICONSET="$WORK/AppIcon.iconset"
mkdir -p "$ICONSET" extension/icons

for size in 16 32 128 256 512; do
    sips -z "$size" "$size" "$SOURCE" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
    retina=$((size * 2))
    sips -z "$retina" "$retina" "$SOURCE" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o Resources/AppIcon.icns

for size in 16 32 48 128; do
    sips -z "$size" "$size" "$SOURCE" --out "extension/icons/icon-${size}.png" >/dev/null
done
scripts/generate-modern-icon.sh
echo "Generated Resources/AppIcon.icns and extension/icons/*.png"
