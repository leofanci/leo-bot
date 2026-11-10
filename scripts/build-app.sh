#!/bin/sh
# Build LeoBot.app's launcher from source. The compiled binary is not tracked in
# git (nobody can review a binary in a pull request), so run this once after
# cloning. Afterwards the post-merge/post-checkout hooks rebuild it via
# scripts/ensure-app.sh whenever webview.swift or start-server.sh change.
# Needs Xcode Command Line Tools (xcode-select --install).
#
# Usage: scripts/build-app.sh [--out DIR]
#   --out DIR  build a copy of the app at DIR/LeoBot.app (for example
#              ~/Applications) instead of in this checkout.
set -eu
cd "$(dirname "$0")/.."
SRCAPP=LeoBot.app
OUT=""
while [ $# -gt 0 ]; do
    case "$1" in
        --out)
            [ $# -ge 2 ] || { echo "build-app.sh: --out needs a folder" >&2; exit 2; }
            OUT="$2"; shift 2 ;;
        *) echo "usage: build-app.sh [--out DIR]" >&2; exit 2 ;;
    esac
done
APP="$SRCAPP"
if [ -n "$OUT" ]; then
    APP="$OUT/LeoBot.app"
    mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
    cp "$SRCAPP/Contents/Info.plist" "$APP/Contents/Info.plist"
    cp "$SRCAPP/Contents/Resources/"* "$APP/Contents/Resources/"
fi
mkdir -p "$APP/Contents/MacOS"
swiftc -O -o "$APP/Contents/MacOS/LeoBot" "$SRCAPP/Contents/Resources/webview.swift" \
    -framework Cocoa -framework WebKit -framework CryptoKit
# Ad-hoc local signature: covers the binary and the bundled start-server.sh.
codesign --force --sign - "$APP"
echo "Built $APP. Open it from Finder or run: open \"$APP\""
