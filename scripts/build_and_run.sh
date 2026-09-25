#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
scripts/build-app.sh debug
if pkill -x SecondCursor 2>/dev/null; then
  for _ in $(seq 1 50); do pgrep -x SecondCursor >/dev/null || break; sleep 0.2; done
fi
if pgrep -x SecondCursor >/dev/null; then echo 'Existing app did not exit gracefully' >&2; exit 1; fi
ditto build/SecondCursor.app "$HOME/Applications/SecondCursor.app"
scripts/install-native-host.sh "$HOME/Applications/SecondCursor.app/Contents/MacOS/sc-native-host"
open "$HOME/Applications/SecondCursor.app"
if [ "${1:-}" = --verify ]; then sleep 1; pgrep -x SecondCursor; fi
