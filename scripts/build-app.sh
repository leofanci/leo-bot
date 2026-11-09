#!/bin/sh
# Build LeoBot.app's launcher from source. The compiled binary is not tracked in
# git (nobody can review a binary in a pull request), so run this once after
# cloning. Afterwards the post-merge/post-checkout hooks rebuild it via
# scripts/ensure-app.sh whenever webview.swift or start-server.sh change.
# Needs Xcode Command Line Tools (xcode-select --install).
set -eu
cd "$(dirname "$0")/.."
APP=LeoBot.app
mkdir -p "$APP/Contents/MacOS"
swiftc -O -o "$APP/Contents/MacOS/LeoBot" "$APP/Contents/Resources/webview.swift" \
    -framework Cocoa -framework WebKit -framework CryptoKit
# Ad-hoc local signature: covers the binary and the bundled start-server.sh.
codesign --force --sign - "$APP"
echo "Built $APP. Open it from Finder or run: open $APP"
