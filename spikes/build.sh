#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p bin
for src in *.swift; do
    swiftc -O "$src" -o "bin/${src%.swift}"
done
clang -fobjc-arc -framework Foundation -framework CoreGraphics vdisplay.m -o bin/vdisplay
echo "built into spikes/bin/"
