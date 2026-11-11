#!/bin/sh
# Build LeoBot.app's launcher from source. The compiled binary is not tracked in
# git (nobody can review a binary in a pull request), so run this once after
# cloning. Afterwards the post-merge/post-checkout hooks rebuild it via
# scripts/ensure-app.sh whenever the app's sources change.
# Needs Xcode Command Line Tools (xcode-select --install).
#
# Usage: scripts/build-app.sh [--out DIR] | --hash
#   --out DIR  build a copy of the app at DIR/LeoBot.app (for example
#              ~/Applications) instead of in this checkout.
#   --hash     print the hash of the app's sources and stop. Every build records it in
#              Contents/Resources/source-hash, so ensure-app.sh rebuilds only on a change.
set -eu
START_DIR="$(pwd)"
cd "$(dirname "$0")/.."
SRCAPP=LeoBot.app

# Everything the built app is made from, this script included (its build flags).
source_hash() {
    cat "$SRCAPP/Contents/Info.plist" "$SRCAPP/Contents/Resources/webview.swift" \
        "$SRCAPP/Contents/Resources/start-server.sh" "$SRCAPP/Contents/Resources/AppIcon.icns" \
        scripts/build-app.sh | shasum -a 256 | cut -d ' ' -f 1
}

# An absolute path: ~ expanded, relative ones taken from where this script was started.
absolute() {
    case "$1" in
        \~) set -- "$HOME" ;;
        \~/*) set -- "$HOME/${1#\~/}" ;;
    esac
    case "$1" in
        /*) printf '%s\n' "${1%/}" ;;
        *) printf '%s\n' "$START_DIR/${1%/}" ;;
    esac
}

OUT=""
while [ $# -gt 0 ]; do
    case "$1" in
        --out)
            [ $# -ge 2 ] || { echo "build-app.sh: --out needs a folder" >&2; exit 2; }
            OUT="$(absolute "$2")"; shift 2 ;;
        --hash) source_hash; exit 0 ;;
        *) echo "usage: build-app.sh [--out DIR] | --hash" >&2; exit 2 ;;
    esac
done
APP="$SRCAPP"
if [ -n "$OUT" ]; then
    APP="$OUT/LeoBot.app"
    mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
    cp "$SRCAPP/Contents/Info.plist" "$APP/Contents/Info.plist"
    cp "$SRCAPP/Contents/Resources/"* "$APP/Contents/Resources/"
    # A Finder/Dock launch carries no environment: record a custom home in the bundle, as an
    # absolute path (the app ignores anything else).
    rm -f "$APP/Contents/Resources/leobot-home"
    if [ -n "${LEOBOT_HOME:-}" ]; then
        HOME_REC="$(absolute "$LEOBOT_HOME")"
        if [ "$HOME_REC" != "$HOME/.leobot" ]; then
            printf '%s\n' "$HOME_REC" > "$APP/Contents/Resources/leobot-home"
        fi
    fi
fi
mkdir -p "$APP/Contents/MacOS"
swiftc -O -o "$APP/Contents/MacOS/LeoBot" "$SRCAPP/Contents/Resources/webview.swift" \
    -framework Cocoa -framework WebKit -framework CryptoKit
source_hash > "$APP/Contents/Resources/source-hash"
# Ad-hoc local signature: covers the binary and the bundled files.
codesign --force --sign - "$APP"
echo "Built $APP. Open it from Finder or run: open \"$APP\""
