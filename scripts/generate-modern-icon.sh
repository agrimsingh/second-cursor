#!/bin/bash
# Compile the native icon stack used on macOS 26 and later.
# Preserve the separate ICNS resource for older macOS and external file viewers.
set -euo pipefail
cd "$(dirname "$0")/.."

WORK="$(mktemp -d "${TMPDIR:-/tmp}/second-cursor-modern-icon.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
xcrun actool Resources/AppIcon.icon \
    --compile "$WORK" \
    --platform macosx \
    --target-device mac \
    --minimum-deployment-target 14.0 \
    --app-icon AppIcon \
    --output-partial-info-plist "$WORK/Info.plist" \
    --output-format human-readable-text \
    --notices --warnings \
    --lightweight-asset-runtime-mode enabled
[ -s "$WORK/Assets.car" ] || { echo "actool did not generate Assets.car" >&2; exit 1; }
cp "$WORK/Assets.car" Resources/Assets.car
echo "Generated Resources/Assets.car; preserved Resources/AppIcon.icns"
