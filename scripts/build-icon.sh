#!/bin/sh
# Rebuild LeoBot.app's AppIcon.icns from dashboard/logo.svg (the one logo
# source). Run after changing the logo; scripts/ensure-app.sh then re-signs
# the app on the next pull, or run scripts/build-app.sh now.
# Needs Xcode Command Line Tools (swift, iconutil).
set -eu
cd "$(dirname "$0")/.."
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
SET="$TMP/AppIcon.iconset"
mkdir "$SET"
swiftc -O -o "$TMP/render" scripts/render-icon.swift -framework AppKit
for n in 16 32 128 256 512; do
    "$TMP/render" dashboard/logo.svg "$SET/icon_${n}x${n}.png" "$n"
    "$TMP/render" dashboard/logo.svg "$SET/icon_${n}x${n}@2x.png" "$((n * 2))"
done
iconutil -c icns "$SET" -o LeoBot.app/Contents/Resources/AppIcon.icns
echo "Wrote LeoBot.app/Contents/Resources/AppIcon.icns"
